---
repo: architecture
path: docs/architecture/aw-app-proxy.md
source: generated
edited: false
checksum: sha256:7f0fa0b9d545f5d94cbdc6b8972c32a2d8433f9c6bc4a853a4c1c75188ce7313
---
# Proxy

- **repo**: aw-app-proxy
- **layer**: app
- **technologies**: python
- **health** (derived): planned

Browser cookie proxy: an HTTP CONNECT proxy the AW Browser tunnels through, plus the cookie-sync API (/sync-cookies, /clear-cookies) that the aw-sync browser extensions (Chrome + iOS/Safari) push the user's real-browser cookies through, and encrypted persistence of chosen cookies in Postgres so they survive a proxy restart. aw-app-browser depends on this app for authentication.

## Connections
- `db` → **postgres** — app-owned tables in the workspace schema
- `http` → **aw-workspace** — routes mounted at /api/apps/proxy

## MCP tools
_none exposed_

## Requirements
### Config vazia cai nas faixas privadas, não em rede aberta
- Given o app é ativado sem allowed_networks configurado
- When o plugin resolve a lista antes de montar o comando do proxy_server (repos/aw-app-proxy/proxy_app/plugin.py::resolve_allowed_networks:46, default em :43)
- Then valem só 127.0.0.0/8, 10.0.0.0/8, 172.16.0.0/12 e 192.168.0.0/16, e uma lista explícita substitui inteira — se a ausência caísse em lista vazia interpretada como "sem restrição", o proxy do workspace ficaria aberto a qualquer origem que alcançasse a porta, com a config parecendo apenas "não preenchida"
- intended_status: `not_implemented` · derived health: `not_implemented`
- tests: `repos/aw-app-proxy/tests/test_plugin.py` (passing)

### O cookie persistido só existe cifrado na tabela
- Given um cookie de navegador é marcado para sobreviver a um restart do proxy
- When a linha é gravada na tabela app__proxy__persisted_cookies, cuja única coluna de valor é value_enc (repos/aw-app-proxy/proxy_app/cookie_store.py::CookieStore.upsert:48, cifra em repos/aw-app-proxy/proxy_app/crypto.py::encrypt:24)
- Then o texto claro não tem coluna onde morar e a leitura com chave errada levanta ValueError nomeando rotação/corrupção em vez de devolver lixo — sem isso as sessões de navegador de todos os sites logados ficam legíveis para qualquer coisa que leia a tabela, e uma chave rotacionada devolveria bytes inválidos como se fossem cookie
- intended_status: `not_implemented` · derived health: `not_implemented`
- tests: `repos/aw-app-proxy/tests/test_cookie_store.py` (passing)

### O proxy continua utilizável com o app de navegador parado
- Given aw-app-browser está desligado e toda sonda CDP volta vazia
- When alguém sincroniza, limpa ou restaura cookies pelas rotas do proxy (repos/aw-app-proxy/proxy_app/routes.py::build_app.clear_browser_cookies:283, ::build_app.sync_cookies:204, ::restore_persisted_cookies:67)
- Then a operação de armazenamento acontece assim mesmo e o restore reporta zero e espera o próximo poll — a regressão que isso trava é a limpeza que devolvia 502 com o browser fora do ar E deixava as linhas no banco, ou seja, o usuário via erro, tentava de novo, e os cookies que ele acabara de "limpar" continuavam lá
- intended_status: `not_implemented` · derived health: `not_implemented`
- tests: `repos/aw-app-proxy/tests/test_routes_browser_offline.py` (passing)
