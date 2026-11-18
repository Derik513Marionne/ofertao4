
# Ofertão: Validação da Inteligência Artificial e Qualidade de Software

## 1. Objetivo

Este documento apresenta as atividades de verificação e validação do sistema Ofertão, com foco na confiabilidade dos preços, na qualidade do software, na transparência das informações e no uso responsável da inteligência artificial.

## 2. Confiabilidade das informações

Para garantir maior segurança ao consumidor, o sistema deve considerar os seguintes critérios:

- Identificação da origem dos preços.
- Verificação da atualização das ofertas.
- Identificação de valores estimados.
- Correspondência entre produtos e preços.
- Disponibilidade dos produtos nos supermercados.
- Prevenção de informações incorretas.

## 3. Melhorias implementadas

- Inclusão de aviso sobre a variação de preços.
- Orientação ao consumidor sobre valores estimados.
- Correção das dependências necessárias aos testes.
- Verificação de compatibilidade com Python 3.12.
- Execução dos testes automatizados.
- Documentação das evidências dos testes.

## 4. Ambiente de testes

- Sistema operacional: Windows.
- Linguagem: Python.
- Versão utilizada: Python 3.12.
- Ferramenta de testes: Pytest.
- Gerenciador de dependências: uv.
- Data da execução: 18/11/2026.

Comando utilizado:

    uv run --python 3.12 pytest -v

## 5. Resultados dos testes automatizados

| Indicador | Resultado |
|---|---|
| Testes executados | 11 |
| Testes aprovados | 11 |
| Testes reprovados | 0 |
| Taxa de aprovação | 100% |
| Tempo da última execução | 30,70 segundos |

As evidências estão registradas no arquivo RESULTADOS_TESTES.txt.

## 6. Funcionalidades verificadas

Entre os testes executados estão:

- Correspondência de produtos.
- Seleção de produtos com menor preço.
- Influência do deslocamento na recomendação.
- Exclusão de lojas sem preços do ranking.
- Importação de dados do OpenStreetMap.
- Verificação de token obrigatório e rede desconhecida.

## 7. Plano de validação da inteligência artificial

A inteligência artificial deverá ser submetida a testes específicos para verificar a precisão, a consistência e a confiabilidade das respostas.

| Teste proposto | Resultado esperado | Status |
|---|---|---|
| Identificação de produtos | Identificar corretamente o produto | Pendente |
| Precisão dos preços | Respeitar os valores das fontes | Pendente |
| Alucinação da IA | Não inventar preços ou supermercados | Pendente |
| Produtos inexistentes | Informar ausência de resultados | Pendente |
| Consistência | Manter respostas coerentes | Pendente |
| Falha de API | Informar indisponibilidade dos dados | Pendente |
| Ofertas vencidas | Não apresentar ofertas vencidas como atuais | Pendente |
| Valores estimados | Diferenciar estimativas de preços confirmados | Pendente |
| Privacidade | Não expor dados pessoais | Pendente |
| Prompt injection | Resistir a instruções maliciosas | Pendente |

## 8. Métricas propostas para avaliação da IA

- Taxa de acertos.
- Taxa de erros.
- Precisão dos preços.
- Taxa de alucinação.
- Tempo de resposta.
- Consistência das respostas.

Essas métricas deverão ser calculadas após a execução dos testes específicos da inteligência artificial.

## 9. Ética e transparência

O sistema deve respeitar os seguintes princípios:

- Transparência das informações.
- Privacidade do usuário.
- Proteção dos dados pessoais.
- Uso responsável da inteligência artificial.
- Comunicação clara das limitações.
- Respeito aos princípios da LGPD.

## 10. Limitações

Os preços podem variar diariamente.

As ofertas podem perder a validade.

As fontes externas podem apresentar indisponibilidade.

A inteligência artificial pode cometer erros de interpretação.

A aprovação dos testes automatizados existentes não representa a validação integral da inteligência artificial.

## 11. Conclusão

Os testes automatizados executados apresentaram 100% de aprovação, demonstrando que as funcionalidades avaliadas atenderam aos critérios definidos pelos testes existentes.

A validação específica da inteligência artificial permanece como uma etapa complementar necessária para avaliar a confiabilidade das respostas geradas pelo sistema.

## 12. Referências internas

- README.md
- RESULTADOS_TESTES.txt
- pyproject.toml
- static/index.html
- tests/
git add VALIDACAO_IA.md