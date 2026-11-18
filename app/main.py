import os
import re
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

import anthropic
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlmodel import Session, col, func, or_, select

from app.catalogo import REGRAS_POR_SLUG, normalizar, sincronizar_catalogo
from app.coletores import cupom, encarte, osm, vtex
from app.coletores.vtex import (
    consultar_termo_publico,
    consultar_termos_em_lote,
    salvar_preco,
    salvar_produto,
    sincronizar_precos_mercados,
)
from app.database import criar_tabelas, engine, get_session
from app.geo import distancia_rua_km
from app.models import Encarte, ItemGenerico, Mercado, Preco, Produto
from app.recommender import recomendar
from app.schemas import PedidoRecomendacao, PrecosDono, RespostaRecomendacao
from app.agente import RequisicaoAgente, RespostaAgente, executar_agente

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
if not STATIC_DIR.exists():
    for _alt in [Path("static").resolve(), Path("/var/task/static").resolve()]:
        if _alt.exists():
            STATIC_DIR = _alt
            break

def _obter_arquivo_estatico(nome: str) -> Path:
    caminho = STATIC_DIR / nome
    if caminho.exists():
        return caminho
    for _alt in [Path("static") / nome, Path("/var/task/static") / nome]:
        if _alt.exists():
            return _alt.resolve()
    return caminho
TOKEN_ADMIN = os.getenv("OFERTAO_ADMIN_TOKEN")


@asynccontextmanager
async def lifespan(_: FastAPI):
    criar_tabelas()
    with Session(engine) as session:
        sincronizar_catalogo(session)
    yield


app = FastAPI(title="Ofertão API", version="0.2.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
@app.get("/api", include_in_schema=False)
@app.get("/api/index.py", include_in_schema=False)
@app.get("/index.py", include_in_schema=False)
def landing() -> FileResponse:
    return FileResponse(_obter_arquivo_estatico("landing.html"))


@app.get("/app", include_in_schema=False)
@app.get("/api/app", include_in_schema=False)
def pagina_principal() -> FileResponse:
    return FileResponse(_obter_arquivo_estatico("index.html"))


@app.get("/api/status")
def status(session: Session = Depends(get_session)):
    """Quantos dados reais o sistema tem hoje."""
    por_fonte = session.exec(select(Preco.fonte, func.count()).group_by(Preco.fonte)).all()
    return {
        "lojas": session.exec(select(func.count()).select_from(Mercado)).one(),
        "lojas_com_preco": session.exec(select(func.count(func.distinct(Preco.mercado_id)))).one(),
        "produtos": session.exec(select(func.count()).select_from(Produto)).one(),
        "precos_por_fonte": dict(por_fonte),
    }


def _mercados_no_raio(session: Session, lat: float | None, lon: float | None, raio_km: float) -> list[Mercado]:
    todos = list(session.exec(select(Mercado).order_by(Mercado.nome)).all())
    if lat is None or lon is None:
        return todos
    return [m for m in todos if distancia_rua_km(lat, lon, m.lat, m.lon) <= max(raio_km, 2.0)]


@app.get("/api/itens")
def listar_itens(
    q: str | None = None,
    ao_vivo: bool = Query(default=False),
    lat: float | None = Query(default=None),
    lon: float | None = Query(default=None),
    raio_km: float = Query(default=15.0),
    session: Session = Depends(get_session),
):
    """Itens genéricos da feira + produtos específicos (consulta apenas nos mercados do raio)."""
    genericos_db = session.exec(select(ItemGenerico).order_by(ItemGenerico.categoria, ItemGenerico.nome)).all()
    termo = (q or "").strip()

    def _buscar_produtos_db() -> list[Produto]:
        if not termo:
            return []
        stmt = select(Produto).order_by(Produto.nome).limit(36)
        palavras = [p for p in re.split(r"\s+", termo) if p]
        for pal in palavras:
            stmt = stmt.where(
                or_(
                    col(Produto.nome).ilike(f"%{pal}%"),
                    col(Produto.marca).ilike(f"%{pal}%"),
                    Produto.ean == termo,
                )
            )
        return list(session.exec(stmt).all())

    produtos_db = _buscar_produtos_db()
    consultou_api = False
    if termo and len(termo) >= 2 and (ao_vivo or len(produtos_db) < 4):
        try:
            mercados_raio = _mercados_no_raio(session, lat, lon, raio_km)
            consultar_termo_publico(session, termo, apenas_catalogo=False, mercados_alvo=mercados_raio)
            consultou_api = True
            produtos_db = _buscar_produtos_db()
        except Exception:
            pass

    if termo:
        tnorm = normalizar(termo)
        genericos_db = [
            g for g in genericos_db
            if tnorm in normalizar(g.nome) or tnorm in normalizar(g.categoria)
        ]

    # Menor preço considerando somente os mercados dentro do raio (se informado)
    if lat is not None and lon is not None:
        mercados_raio = _mercados_no_raio(session, lat, lon, raio_km)
        if mercados_raio:
            stmt_min = (
                select(Preco.produto_id, func.min(Preco.preco))
                .where(col(Preco.mercado_id).in_([m.id for m in mercados_raio]))
                .group_by(Preco.produto_id)
            )
            min_por_prod = dict(session.exec(stmt_min).all())
        else:
            min_por_prod = {}
    else:
        stmt_min = select(Preco.produto_id, func.min(Preco.preco)).group_by(Preco.produto_id)
        min_por_prod = dict(session.exec(stmt_min).all())

    min_por_grupo: dict[int, float] = {}
    img_por_grupo: dict[int, str] = {}
    for p in session.exec(select(Produto).where(col(Produto.grupo_id).is_not(None))).all():
        mp = min_por_prod.get(p.id)
        if mp is not None and (p.grupo_id not in min_por_grupo or mp < min_por_grupo[p.grupo_id]):
            min_por_grupo[p.grupo_id] = mp
        if p.imagem_url and p.grupo_id not in img_por_grupo:
            img_por_grupo[p.grupo_id] = p.imagem_url

    genericos_out = [
        {
            "id": g.id,
            "slug": g.slug,
            "nome": g.nome,
            "categoria": g.categoria,
            "menor_preco": round(min_por_grupo[g.id], 2) if g.id in min_por_grupo else None,
            "imagem_url": img_por_grupo.get(g.id),
        }
        for g in genericos_db
    ]
    produtos_out = [
        {
            "id": p.id,
            "ean": p.ean,
            "nome": p.nome,
            "marca": p.marca,
            "imagem_url": p.imagem_url,
            "grupo_id": p.grupo_id,
            "menor_preco": round(min_por_prod[p.id], 2) if p.id in min_por_prod else None,
        }
        for p in produtos_db
    ]
    return {"genericos": genericos_out, "produtos": produtos_out, "consultou_api": consultou_api}


@app.post("/api/consultar")
def consultar_ao_vivo(
    q: str = Query(..., min_length=2),
    lat: float | None = Query(default=None),
    lon: float | None = Query(default=None),
    raio_km: float = Query(default=15.0),
    session: Session = Depends(get_session),
):
    """Força consulta em tempo real nas APIs públicas apenas para os mercados no raio."""
    mercados_raio = _mercados_no_raio(session, lat, lon, raio_km)
    stats = consultar_termo_publico(session, q, apenas_catalogo=False, mercados_alvo=mercados_raio)
    return {"ok": True, **stats}


@app.get("/api/localidades")
def buscar_localidades(q: str = Query(..., min_length=2)):
    """Busca cidades, bairros, endereços ou CEPs em todo o Brasil."""
    try:
        return osm.buscar_localidade_brasil(q)
    except Exception:
        return []


@app.get("/api/mercados", response_model=list[Mercado])
def listar_mercados(
    lat: float | None = Query(default=None),
    lon: float | None = Query(default=None),
    raio_km: float = Query(default=15.0),
    session: Session = Depends(get_session),
):
    if lat is not None and lon is not None:
        proximos = _mercados_no_raio(session, lat, lon, raio_km)
        if len(proximos) < 5:
            try:
                osm.importar_mercados_regiao(session, lat, lon, min(raio_km, 15.0))
                proximos = _mercados_no_raio(session, lat, lon, raio_km)
            except Exception:
                pass
        return proximos

    todos = list(session.exec(select(Mercado).order_by(Mercado.nome)).all())
    if not todos:
        try:
            osm.importar_mercados(session)
            todos = list(session.exec(select(Mercado).order_by(Mercado.nome)).all())
        except Exception:
            pass
    return todos


@app.post("/api/recomendar", response_model=RespostaRecomendacao)
def recomendar_mercado(pedido: PedidoRecomendacao, session: Session = Depends(get_session)):
    # 1. Filtra estritamente os mercados dentro do raio pedido
    mercados_atuais = list(session.exec(select(Mercado)).all())
    proximos = [
        m for m in mercados_atuais
        if distancia_rua_km(pedido.lat, pedido.lon, m.lat, m.lon) <= pedido.raio_km
    ]
    if len(proximos) < 3 and (not mercados_atuais or any(m.osm_id for m in mercados_atuais)):
        try:
            osm.importar_mercados_regiao(session, pedido.lat, pedido.lon, min(pedido.raio_km, 15.0))
            mercados_atuais = list(session.exec(select(Mercado)).all())
            proximos = [
                m for m in mercados_atuais
                if distancia_rua_km(pedido.lat, pedido.lon, m.lat, m.lon) <= pedido.raio_km
            ]
        except Exception:
            pass

    if not proximos:
        return recomendar(session, pedido)

    # 2. Consulta em lote (paralelo) apenas os itens do carrinho que ainda não têm cotação
    grupo_ids = [it.grupo_id for it in pedido.itens if it.grupo_id is not None]
    produto_ids = [it.produto_id for it in pedido.itens if it.produto_id is not None]
    if grupo_ids and any(m.osm_id for m in proximos):
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
                consultar_termos_em_lote(session, termos_faltantes, mercados_alvo=proximos, apenas_catalogo=False)
            except Exception:
                pass

    # 3. Sincroniza apenas os produtos do carrinho e apenas para os mercados dentro do raio
    if any(m.osm_id for m in proximos):
        sincronizar_precos_mercados(session, proximos, grupo_ids=grupo_ids, produto_ids=produto_ids)

    return recomendar(session, pedido)


@app.post("/api/mercados/{mercado_id}/cupom")
async def enviar_cupom(
    mercado_id: int,
    foto: UploadFile | None = File(default=None),
    texto: str | None = Form(default=None),
    session: Session = Depends(get_session),
):
    """Usuário envia a foto (ou o texto) do cupom fiscal de uma compra naquela loja."""
    mercado = session.get(Mercado, mercado_id)
    if not mercado:
        raise HTTPException(404, "Loja não encontrada")
    imagem = await foto.read() if foto else None
    if imagem and len(imagem) > 8 * 1024 * 1024:
        raise HTTPException(413, "Foto muito grande (máx. 8 MB)")
    try:
        dados = cupom.extrair(imagem=imagem, media_type=(foto.content_type if foto else "image/jpeg"),
                              texto=texto)
    except ValueError as e:
        raise HTTPException(422, str(e))
    except (encarte.SemCredencial, anthropic.AuthenticationError):
        raise HTTPException(503, "Leitura de cupons indisponível: configure ANTHROPIC_API_KEY")
    except anthropic.APIError as e:
        raise HTTPException(502, f"Falha ao ler o cupom: {e.message}")
    gravados = cupom.importar(session, mercado, dados)
    return {"itens_gravados": gravados, "cupom": dados}


def _exigir_token(x_token: str = Header(default="")) -> None:
    if not TOKEN_ADMIN or not secrets.compare_digest(x_token, TOKEN_ADMIN):
        raise HTTPException(401, "Token inválido")


@app.get("/admin", include_in_schema=False)
@app.get("/api/admin", include_in_schema=False)
def pagina_admin() -> FileResponse:
    return FileResponse(_obter_arquivo_estatico("admin.html"))


@app.get("/api/redes")
def listar_redes(session: Session = Depends(get_session)):
    """Redes com lojas na Grande Natal (para associar encartes)."""
    linhas = session.exec(
        select(Mercado.rede, func.count()).where(Mercado.rede.is_not(None)).group_by(Mercado.rede)
    ).all()
    return [{"rede": r, "lojas": n} for r, n in sorted(linhas)]


@app.get("/api/encartes", response_model=list[Encarte])
def listar_encartes(session: Session = Depends(get_session)):
    return session.exec(select(Encarte).order_by(col(Encarte.processado_em).desc()).limit(50)).all()


@app.post("/api/encartes", dependencies=[Depends(_exigir_token)])
def enviar_encarte(
    rede: str = Form(...),
    arquivo: UploadFile = File(...),
    url_origem: str | None = Form(default=None),
    session: Session = Depends(get_session),
):
    """Envia um encarte (PDF ou imagem) de uma rede; a IA extrai as ofertas."""
    conteudo = arquivo.file.read(encarte.MAX_BYTES + 1)
    if len(conteudo) > encarte.MAX_BYTES:
        raise HTTPException(413, "Arquivo muito grande")
    tipo = arquivo.content_type or "application/octet-stream"
    if tipo != "application/pdf" and not tipo.startswith("image/"):
        raise HTTPException(415, "Envie PDF ou imagem")
    try:
        r = encarte.processar(session, rede, conteudo, tipo, url_origem)
    except ValueError as e:
        raise HTTPException(422, str(e))
    except (encarte.SemCredencial, anthropic.AuthenticationError):
        raise HTTPException(503, "Leitura indisponível: configure ANTHROPIC_API_KEY")
    except anthropic.APIError as e:
        raise HTTPException(502, f"Falha ao ler o encarte: {e.message}")
    return r


def _gravar_precos_dono(session: Session, mercado_ids: list[int], corpo: PrecosDono) -> dict:
    grupos = sincronizar_catalogo(session)
    for item in corpo.itens:
        prod = salvar_produto(session, item.ean, item.nome, None, None, grupos)
        if prod.grupo_id is None and item.item_generico in REGRAS_POR_SLUG:
            prod.grupo_id = grupos[item.item_generico]
        for mercado_id in mercado_ids:
            salvar_preco(session, mercado_id, prod.id, item.preco, "dono", item.promocao)
    session.commit()
    return {"itens_gravados": len(corpo.itens), "lojas": len(mercado_ids)}


@app.post("/api/mercados/{mercado_id}/precos")
def precos_do_dono(
    mercado_id: int,
    corpo: PrecosDono,
    session: Session = Depends(get_session),
    _: None = Depends(_exigir_token),
):
    """Painel do dono: a loja informa seus preços. Protegido por OFERTAO_ADMIN_TOKEN."""
    if not session.get(Mercado, mercado_id):
        raise HTTPException(404, "Loja não encontrada")
    return _gravar_precos_dono(session, [mercado_id], corpo)


@app.post("/api/redes/{rede}/precos")
def precos_da_rede(
    rede: str,
    corpo: PrecosDono,
    session: Session = Depends(get_session),
    _: None = Depends(_exigir_token),
):
    """Cadastro manual de preços válidos em todas as lojas de uma rede (ex.: copiados do encarte)."""
    ids = session.exec(select(Mercado.id).where(Mercado.rede == rede)).all()
    if not ids:
        raise HTTPException(404, "Nenhuma loja dessa rede")
    return _gravar_precos_dono(session, list(ids), corpo)

from app.agente import router as agente_router
app.include_router(agente_router)
