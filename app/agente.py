"""Agente Autônomo do Ofertão (Arquitetura ReAct / Tool Calling)."""

import json
import os
import re
from typing import Any

import anthropic
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlmodel import Session, col, or_, select

from app.catalogo import REGRAS_POR_SLUG, normalizar
from app.coletores import osm, vtex
from app.coletores.vtex import consultar_termos_em_lote, sincronizar_precos_mercados
from app.database import get_session
from app.geo import distancia_rua_km
from app.models import ItemGenerico, Mercado, Preco, Produto
from app.recommender import recomendar
from app.schemas import ItemCarrinho, PedidoRecomendacao

router = APIRouter()


class RequisicaoAgente(BaseModel):
    mensagem: str = Field(..., min_length=2, description="Pedido em linguagem natural do usuário")
    lat: float | None = None
    lon: float | None = None
    raio_km: float = 15.0
    orcamento_max: float | None = None


class PassoAgente(BaseModel):
    ferramenta: str
    entrada: dict[str, Any]
    resultado_resumido: str


class RespostaAgente(BaseModel):
    resposta: str
    passos: list[PassoAgente]
    recomendacao: dict[str, Any] | None = None
    modo: str


def tool_buscar_localizacao(session: Session, consulta: str) -> dict[str, Any]:
    """Busca latitude e longitude de um bairro ou cidade no banco ou via OSM."""
    q_norm = normalizar(consulta)
    mercados = session.exec(select(Mercado)).all()
    for m in mercados:
        if q_norm and (q_norm in normalizar(m.bairro) or q_norm in normalizar(m.cidade)):
            return {
                "encontrado": True,
                "local": f"{m.bairro or m.cidade} ({m.nome})",
                "lat": m.lat,
                "lon": m.lon,
                "fonte": "banco_sqlmodel",
            }
    try:
        locais = osm.buscar_localidade_brasil(consulta)
        if locais:
            primeiro = locais[0]
            return {
                "encontrado": True,
                "local": primeiro.get("nome") or consulta,
                "lat": float(primeiro["lat"]),
                "lon": float(primeiro["lon"]),
                "fonte": "openstreetmap",
            }
    except Exception:
        pass
    return {
        "encontrado": False,
        "local": "Natal/RN (Padrão)",
        "lat": -5.8275,
        "lon": -35.2050,
        "fonte": "padrao_sistema",
    }


def tool_buscar_produtos_sql(session: Session, termo: str) -> dict[str, Any]:
    """Consulta o banco de dados via SQLModel procurando itens da cesta ou produtos."""
    tnorm = normalizar(termo)
    genericos = session.exec(select(ItemGenerico)).all()
    gen_matches = [
        {"grupo_id": g.id, "nome": g.nome, "categoria": g.categoria}
        for g in genericos
        if tnorm in normalizar(g.nome)
        or any(p in normalizar(g.nome) for p in tnorm.split() if len(p) >= 3)
    ]

    stmt = (
        select(Produto)
        .where(or_(col(Produto.nome).ilike(f"%{termo}%"), col(Produto.marca).ilike(f"%{termo}%")))
        .limit(5)
    )
    produtos = session.exec(stmt).all()
    prod_matches = [
        {"produto_id": p.id, "nome": p.nome, "marca": p.marca, "grupo_id": p.grupo_id}
        for p in produtos
    ]

    if not gen_matches and not prod_matches and len(termo) >= 3:
        try:
            vtex.consultar_termo_publico(session, termo, apenas_catalogo=False)
            produtos = session.exec(stmt).all()
            prod_matches = [
                {"produto_id": p.id, "nome": p.nome, "marca": p.marca, "grupo_id": p.grupo_id}
                for p in produtos
            ]
        except Exception:
            pass

    return {
        "termo": termo,
        "itens_genericos": gen_matches[:4],
        "produtos_especificos": prod_matches[:4],
    }


def tool_calcular_melhor_compra(
    session: Session,
    lat: float,
    lon: float,
    itens: list[dict[str, Any]],
    raio_km: float = 15.0,
) -> dict[str, Any]:
    """Garante mercados/preços na região e aciona o motor recommender.py."""
    carrinho: list[ItemCarrinho] = []
    for it in itens:
        qtd = float(it.get("quantidade", 1))
        if it.get("grupo_id"):
            carrinho.append(ItemCarrinho(grupo_id=int(it["grupo_id"]), quantidade=qtd))
        elif it.get("produto_id"):
            carrinho.append(ItemCarrinho(produto_id=int(it["produto_id"]), quantidade=qtd))

    if not carrinho:
        return {"erro": "Nenhum item válido para calcular."}

    # Garante que há mercados e preços sincronizados no raio (mesma lógica do main.py)
    mercados_atuais = list(session.exec(select(Mercado)).all())
    proximos = [
        m for m in mercados_atuais if distancia_rua_km(lat, lon, m.lat, m.lon) <= raio_km
    ]
    if len(proximos) < 3 and (not mercados_atuais or any(m.osm_id for m in mercados_atuais)):
        try:
            osm.importar_mercados_regiao(session, lat, lon, min(raio_km, 15.0))
            mercados_atuais = list(session.exec(select(Mercado)).all())
            proximos = [
                m for m in mercados_atuais if distancia_rua_km(lat, lon, m.lat, m.lon) <= raio_km
            ]
        except Exception:
            pass

    grupo_ids = [it.grupo_id for it in carrinho if it.grupo_id is not None]
    produto_ids = [it.produto_id for it in carrinho if it.produto_id is not None]

    if proximos and grupo_ids and any(m.osm_id for m in proximos):
        termos_faltantes: list[str] = []
        for g in session.exec(select(ItemGenerico).where(col(ItemGenerico.id).in_(grupo_ids))).all():
            tem_preco = session.exec(
                select(Preco.id)
                .join(Produto, Produto.id == Preco.produto_id)
                .where(Produto.grupo_id == g.id)
                .limit(1)
            ).first()
            if not tem_preco:
                regra = REGRAS_POR_SLUG.get(g.slug)
                termos_faltantes.append(vtex.termo_amplo(regra.busca) if regra else g.nome)
        if termos_faltantes:
            try:
                consultar_termos_em_lote(
                    session, termos_faltantes, mercados_alvo=proximos, apenas_catalogo=False
                )
            except Exception:
                pass

    if proximos and any(m.osm_id for m in proximos):
        try:
            sincronizar_precos_mercados(
                session, proximos, grupo_ids=grupo_ids, produto_ids=produto_ids
            )
        except Exception:
            pass

    pedido = PedidoRecomendacao(
        lat=lat,
        lon=lon,
        itens=carrinho,
        raio_km=raio_km,
        permitir_divisao=True,
    )
    resultado = recomendar(session, pedido)
    return resultado.model_dump(mode="json")


TOOLS_SCHEMA: list[dict[str, Any]] = [
    {
        "name": "buscar_localizacao",
        "description": "Converte o nome de um bairro ou cidade em coordenadas GPS (lat, lon).",
        "input_schema": {
            "type": "object",
            "properties": {
                "consulta": {"type": "string", "description": "Nome do bairro ou cidade"}
            },
            "required": ["consulta"],
        },
    },
    {
        "name": "buscar_produtos_sql",
        "description": "Busca itens genéricos da feira ou produtos específicos no banco SQLModel.",
        "input_schema": {
            "type": "object",
            "properties": {
                "termo": {"type": "string", "description": "Nome do produto desejado"}
            },
            "required": ["termo"],
        },
    },
    {
        "name": "calcular_melhor_compra",
        "description": "Calcula qual supermercado é mais barato somando produtos e deslocamento.",
        "input_schema": {
            "type": "object",
            "properties": {
                "lat": {"type": "number"},
                "lon": {"type": "number"},
                "raio_km": {"type": "number", "default": 15.0},
                "itens": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "grupo_id": {"type": "integer"},
                            "produto_id": {"type": "integer"},
                            "quantidade": {"type": "number", "default": 1},
                        },
                    },
                },
            },
            "required": ["lat", "lon", "itens"],
        },
    },
]


def executar_agente(session: Session, req: RequisicaoAgente) -> RespostaAgente:
    chave_api = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if chave_api:
        try:
            return _executar_com_claude(session, req, chave_api)
        except Exception:
            pass
    return _executar_agente_local(session, req)


def _executar_com_claude(session: Session, req: RequisicaoAgente, api_key: str) -> RespostaAgente:
    client = anthropic.Anthropic(api_key=api_key)
    passos: list[PassoAgente] = []
    ultima_recomendacao: dict[str, Any] | None = None

    contexto_gps = (
        f"Coordenadas atuais: lat={req.lat}, lon={req.lon}, raio={req.raio_km}km."
        if req.lat is not None and req.lon is not None
        else "Sem coordenadas GPS informadas (use buscar_localizacao ou Natal/RN: lat=-5.8275, lon=-35.2050)."
    )
    orc_txt = f"Orçamento máximo: R$ {req.orcamento_max:.2f}." if req.orcamento_max else ""

    system_prompt = (
        "Você é o Agente Inteligente do Ofertão. Use as ferramentas para consultar o banco SQLModel, "
        "encontrar os IDs dos itens pedidos, calcular o mercado mais barato considerando o deslocamento "
        "e responder em português claro e direto."
    )

    mensagens: list[dict[str, Any]] = [
        {"role": "user", "content": f"{contexto_gps} {orc_txt}\nPedido: {req.mensagem}"}
    ]

    for _ in range(6):
        resp = client.messages.create(
            model="claude-3-5-sonnet-latest",
            max_tokens=1024,
            system=system_prompt,
            tools=TOOLS_SCHEMA,  # type: ignore[arg-type]
            messages=mensagens,  # type: ignore[arg-type]
        )

        if resp.stop_reason != "tool_use":
            partes_texto = [getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text"]
            texto_final = "".join(partes_texto).strip()
            return RespostaAgente(
                resposta=texto_final or "Análise concluída com sucesso!",
                passos=passos,
                recomendacao=ultima_recomendacao,
                modo="claude_tool_calling",
            )

        mensagens.append({"role": "assistant", "content": resp.content})
        resultados_tools: list[dict[str, Any]] = []

        for bloco in resp.content:
            if getattr(bloco, "type", "") != "tool_use":
                continue
            nome_tool = str(getattr(bloco, "name", ""))
            entrada_raw = getattr(bloco, "input", None)
            args: dict[str, Any] = entrada_raw if isinstance(entrada_raw, dict) else {}
            bloco_id = str(getattr(bloco, "id", ""))

            if nome_tool == "buscar_localizacao":
                res = tool_buscar_localizacao(session, str(args.get("consulta", "")))
                resumo = f"Local: {res['local']} ({res['lat']:.4f}, {res['lon']:.4f})"
            elif nome_tool == "buscar_produtos_sql":
                res = tool_buscar_produtos_sql(session, str(args.get("termo", "")))
                qtd_g = len(res["itens_genericos"])
                qtd_p = len(res["produtos_especificos"])
                resumo = f"Encontrados {qtd_g} grupos genéricos e {qtd_p} produtos no SQLModel"
            elif nome_tool == "calcular_melhor_compra":
                res = tool_calcular_melhor_compra(
                    session,
                    float(args.get("lat", req.lat or -5.8275)),
                    float(args.get("lon", req.lon or -35.2050)),
                    list(args.get("itens", [])),
                    float(args.get("raio_km", req.raio_km)),
                )
                ultima_recomendacao = res
                melhor = res.get("melhor")
                resumo = (
                    f"Melhor loja: {melhor['nome']} (Total: R$ {melhor['custo_total']:.2f})"
                    if melhor
                    else str(res.get("explicacao", "Sem lojas com dados"))
                )
            else:
                res = {"erro": "Ferramenta desconhecida"}
                resumo = "Erro"

            passos.append(PassoAgente(ferramenta=nome_tool, entrada=args, resultado_resumido=resumo))
            resultados_tools.append(
                {
                    "type": "tool_result",
                    "tool_use_id": bloco_id,
                    "content": json.dumps(res, ensure_ascii=False),
                }
            )

        mensagens.append({"role": "user", "content": resultados_tools})

    return RespostaAgente(
        resposta="Concluí a busca nos mercados próximos. Confira o resultado detalhado abaixo!",
        passos=passos,
        recomendacao=ultima_recomendacao,
        modo="claude_tool_calling",
    )


def _executar_agente_local(session: Session, req: RequisicaoAgente) -> RespostaAgente:
    """Agente Autônomo Local (Funciona sem chave de API externa)."""
    passos: list[PassoAgente] = []
    texto_norm = normalizar(req.mensagem)

    orcamento = req.orcamento_max
    if orcamento is None:
        m_orc = re.search(
            r"(?:r\$|reais|orcamento|tenho|gastar)\s*(\d+(?:[.,]\d+)?)|(\d+(?:[.,]\d+)?)\s*reais",
            req.mensagem.lower(),
        )
        if m_orc:
            val = (m_orc.group(1) or m_orc.group(2)).replace(",", ".")
            orcamento = float(val)

    lat, lon = req.lat, req.lon
    if lat is None or lon is None:
        m_loc = re.search(
            r"\b(?:em|no|na|perto de|bairro)\s+([a-zA-ZÀ-ÿ\s]{3,20})",
            req.mensagem,
            re.IGNORECASE,
        )
        termo_loc = m_loc.group(1).strip() if m_loc else "Natal"
        res_loc = tool_buscar_localizacao(session, termo_loc)
        lat, lon = float(res_loc["lat"]), float(res_loc["lon"])
        passos.append(
            PassoAgente(
                ferramenta="buscar_localizacao",
                entrada={"consulta": termo_loc},
                resultado_resumido=f"{res_loc['local']} (lat={lat:.4f}, lon={lon:.4f})",
            )
        )
    else:
        passos.append(
            PassoAgente(
                ferramenta="obter_gps_usuario",
                entrada={"lat": round(lat, 4), "lon": round(lon, 4)},
                resultado_resumido=f"Coordenadas do mapa ativas ({lat:.4f}, {lon:.4f})",
            )
        )

    todos_genericos = session.exec(select(ItemGenerico)).all()
    itens_escolhidos: list[dict[str, Any]] = []
    nomes_escolhidos: list[str] = []

    sinonimos = {
        "arroz": "arroz-5kg",
        "feijao": "feijao-carioca-1kg",
        "cafe": "cafe-250g",
        "acucar": "acucar-cristal-1kg",
        "oleo": "oleo-soja-900ml",
        "leite": "leite-uht-1l",
        "macarrao": "macarrao-espaguete-500g",
        "espaguete": "macarrao-espaguete-500g",
        "molho": "molho-tomate-340g",
        "frango": "frango-inteiro-kg",
        "ovo": "ovos-30",
        "ovos": "ovos-30",
        "manteiga": "manteiga-200g",
        "margarina": "margarina-500g",
        "pao": "pao-forma",
        "biscoito": "biscoito-cream-cracker",
        "bolacha": "biscoito-cream-cracker",
        "flocao": "flocao-500g",
        "cuscuz": "flocao-500g",
        "farinha": "farinha-mandioca-1kg",
        "sal": "sal-1kg",
        "sardinha": "sardinha-lata",
        "detergente": "detergente-500ml",
        "sabao": "sabao-po-1kg",
        "agua sanitaria": "agua-sanitaria-1l",
        "papel higienico": "papel-higienico-12",
        "creme dental": "creme-dental-90g",
        "pasta de dente": "creme-dental-90g",
        "sabonete": "sabonete-85g",
    }

    mapa_slug = {g.slug: g for g in todos_genericos}
    mapa_id_nome = {g.id: g.nome for g in todos_genericos if g.id is not None}
    slugs_adicionados: set[str] = set()

    for palavra, slug in sinonimos.items():
        if re.search(rf"\b{palavra}s?\b", texto_norm) and slug in mapa_slug and slug not in slugs_adicionados:
            g = mapa_slug[slug]
            if g.id is None:
                continue
            m_qtd = re.search(
                rf"(\d+)\s*(?:pacotes?|caixas?|unidades?|kg|litros?)?\s*(?:de\s+)?{palavra}",
                texto_norm,
            )
            qtd = float(m_qtd.group(1)) if m_qtd else 1.0
            itens_escolhidos.append({"grupo_id": g.id, "quantidade": qtd})
            nomes_escolhidos.append(f"{int(qtd)}x {g.nome}")
            slugs_adicionados.add(slug)

    if not itens_escolhidos:
        padrao_cesta = [
            "arroz-5kg",
            "feijao-carioca-1kg",
            "cafe-250g",
            "oleo-soja-900ml",
            "leite-uht-1l",
            "macarrao-espaguete-500g",
        ]
        for slug in padrao_cesta:
            if slug in mapa_slug and mapa_slug[slug].id is not None:
                g = mapa_slug[slug]
                itens_escolhidos.append({"grupo_id": g.id, "quantidade": 1.0})
                nomes_escolhidos.append(f"1x {g.nome}")

    passos.append(
        PassoAgente(
            ferramenta="buscar_produtos_sql",
            entrada={"mensagem_analisada": req.mensagem},
            resultado_resumido=f"Itens mapeados no SQLModel: {', '.join(nomes_escolhidos)}",
        )
    )

    rec = tool_calcular_melhor_compra(session, lat, lon, itens_escolhidos, req.raio_km)
    melhor = rec.get("melhor")
    passos.append(
        PassoAgente(
            ferramenta="calcular_melhor_compra",
            entrada={"lat": round(lat, 4), "lon": round(lon, 4), "qtd_itens": len(itens_escolhidos)},
            resultado_resumido=(
                f"Melhor opção: {melhor['nome']} — R$ {melhor['custo_total']:.2f}"
                if melhor
                else str(rec.get("explicacao", "Nenhuma loja com preços cadastrados no raio."))
            ),
        )
    )

    aviso_orcamento = ""
    if orcamento and melhor and float(melhor["custo_total"]) > orcamento:
        custo_original = float(melhor["custo_total"])
        itens_ordenados = sorted(melhor.get("itens", []), key=lambda x: float(x["subtotal"]), reverse=True)
        if len(itens_escolhidos) > 1 and itens_ordenados:
            item_caro = str(itens_ordenados[0]["pedido"])
            itens_otimizados = [
                it
                for it in itens_escolhidos
                if mapa_id_nome.get(int(it["grupo_id"]), "") != item_caro
            ]
            if itens_otimizados:
                rec_otim = tool_calcular_melhor_compra(session, lat, lon, itens_otimizados, req.raio_km)
                melhor_otim = rec_otim.get("melhor")
                if melhor_otim:
                    passos.append(
                        PassoAgente(
                            ferramenta="otimizar_orcamento",
                            entrada={"orcamento_max": orcamento, "custo_anterior": custo_original},
                            resultado_resumido=f"Removido '{item_caro}' -> Novo total: R$ {melhor_otim['custo_total']:.2f}",
                        )
                    )
                    aviso_orcamento = (
                        f" 💡 **Análise de Orçamento (Limite: R$ {orcamento:.2f}):** A lista completa ficaria em "
                        f"R$ {custo_original:.2f}. Para caber no seu bolso, retirando **{item_caro}**, "
                        f"o total cai para **R$ {melhor_otim['custo_total']:.2f}** no {melhor_otim['nome']}!"
                    )

    if not melhor:
        resposta_txt = (
            f"Identifiquei os itens ({', '.join(nomes_escolhidos)}), mas "
            f"{rec.get('explicacao', 'não encontrei preços próximos.')}"
        )
    else:
        resposta_txt = (
            f"🤖 **Análise do Agente concluída!** Para a sua lista ({', '.join(nomes_escolhidos)}), "
            f"{rec.get('explicacao', '')}{aviso_orcamento}"
        )

    return RespostaAgente(
        resposta=resposta_txt,
        passos=passos,
        recomendacao=rec,
        modo="agente_heuristico_local",
    )


@router.post("/api/agente", response_model=RespostaAgente)
def rota_conversar_com_agente(
    req: RequisicaoAgente,
    session: Session = Depends(get_session),
) -> RespostaAgente:
    """Endpoint do Agente de IA acoplado via APIRouter."""
    return executar_agente(session, req)