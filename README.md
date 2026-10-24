# Ponte

**Seu mundo. Seus amigos.** Um aplicativo para Windows que compartilha a conexão de um mundo do Minecraft Java com amigos pela internet.

## Baixar e jogar

Baixe **Ponte.exe** na [versão mais recente](https://github.com/OppressorLiber/ponte/releases/latest). O aplicativo é portátil: não precisa instalar Python ou um driver de rede.

1. O anfitrião abre seu mundo e escolhe **Abrir para LAN** no Minecraft.
2. No Ponte, informa a porta exibida no chat e cria um convite.
3. O amigo abre o Ponte, entra com o convite e copia o endereço local apresentado.
4. No Minecraft do amigo: **Multijogador → Conexão direta → colar endereço**.

Mantenha o jogo e o Ponte abertos durante a partida. Os jogadores precisam usar versões e modpacks compatíveis entre si. O túnel transporta uma porta TCP do jogo; mods que usam portas adicionais podem exigir suporte próprio. Esta ferramenta não altera saves, mods, launcher ou contas.

## Atualizações

A partir da versão **0.2.0**, o Ponte verifica automaticamente o GitHub ao iniciar e a cada seis horas. Quando a sessão está encerrada, baixa a nova versão em segundo plano e oferece **Reiniciar e atualizar**. A partida ativa nunca é encerrada para instalar uma atualização.

O arquivo recebido precisa corresponder a um manifesto assinado com Ed25519, ao tamanho informado e ao SHA-256. Um processo separado troca o executável, preserva preferências e confirma que a nova versão iniciou; se a inicialização falhar, restaura a versão anterior. Não há token do GitHub dentro do aplicativo.

Quem ainda usa **0.1.0** precisa baixar a versão mais recente uma vez, pois aquela primeira versão não contém o atualizador.

### Indicador de latência

A versão **0.2.1** usa relógio de alta precisão. Valores menores que um milissegundo aparecem como `<1 ms`; amostras antigas deixam de ser apresentadas como atuais. O número mede a ida e volta do aplicativo, incluindo filas e processamento, e pode variar sem causar travadas no jogo. Passe o mouse sobre a latência de um participante para ver mediana e p95; o diagnóstico inclui o histórico recente, timeouts de sondas e atraso do loop local. Timeout de uma sonda não representa uma porcentagem de perda de pacotes da rede.

### Transporte sob carga — 0.2.2

Esta versão muda o envio dos dados do jogo. Há uma fila compartilhada com admissão justa entre os fluxos TCP, orçamento adaptativo de bytes aguardando confirmação e blocos de envio de 4 KiB. Créditos e encerramento continuam confiáveis. O protocolo de recepção aceita os blocos das versões anteriores; recomendamos atualizar os dois PCs para melhorar o envio nos dois sentidos.

O objetivo é reduzir o acúmulo de dados que faz pedidos pequenos esperarem atrás de transferências grandes. O loop de rede também usa relógio preciso para os prazos do transporte. Picos a partir de 250 ms geram até três diagnósticos locais rotativos, sem convite, nomes, SDP ou IP remoto, para investigar a sessão real.

Uma comparação controlada com TCP, ICE, DTLS e SCTP reais, upload/download concorrentes e pedidos TCP de 64 bytes mostrou:

| Caminho simulado | p95 anterior sob carga | p95 novo sob carga |
|---|---:|---:|
| 2 Mbit/s por sentido, RTT 40 ms | 3.078 ms | 850 ms |
| 20 Mbit/s por sentido, RTT 100 ms | 1.377 ms | 856 ms |

São médias dos percentis de duas repetições por cenário, em ordem alternada, com 16 MB conferidos por SHA-256 e 442 respostas de teste íntegras. No caminho lento a vazão aumentou; no rápido o upload médio caiu cerca de 14% e o download cerca de 12%. É uma troca em favor de menor espera sob carga, não uma promessa de aumentar a velocidade máxima em toda rede. O relatório bruto acompanha a release. O teste usa um caminho moldado no mesmo PC e não substitui a validação entre as casas dos jogadores.

Para reproduzir: `python qa_transport.py --repetitions 2 --bulk-bytes 1000000 --candidate-window 131072 --output qa-transport.json`.

## Como a conexão funciona

WebRTC DataChannels confiáveis e ordenados transportam os bytes do TCP com criptografia DTLS. ICE/STUN tentam estabelecer o caminho direto, sem abrir portas manualmente no roteador. Não há TURN ou retransmissão da partida. Algumas combinações de NAT/firewall podem impedir a conexão direta.

O encontro inicial usa [ntfy.sh](https://ntfy.sh), somente para sinalização comprimida e criptografada ponta a ponta. Convites contêm um segredo aleatório de 256 bits; tópicos derivam de HMAC, e mensagens usam HKDF + ChaCha20Poly1305, com proteção contra repetição. O provedor vê metadados de HTTP e tópicos opacos; os dados da partida não passam por ele. O serviço possui cotas e disponibilidade próprias.

## Desenvolvimento

Python 3.12, Windows x64:

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements-build.txt
.venv\Scripts\python entrypoint.py
.venv\Scripts\python -m unittest discover -s tests -v
.\build.ps1 -Python .venv\Scripts\python.exe
```

Os testes exercitam bytes, half-close, várias conexões, cancelamento, sinais criptografados, atualizações assinadas, rollback e UI. Testes locais e consulta de status do Minecraft não equivalem a uma partida entre duas redes externas; essa validação depende das redes dos jogadores.

O botão **Diagnóstico** salva um relatório em `%LOCALAPPDATA%\Ponte\Diagnosticos`, sem convite, SDP, endereço IP remoto ou conteúdo do mundo.

## Publicação

`release.py` prepara os arquivos de uma release e assina seu manifesto. A chave privada de assinatura fica fora do repositório, no armazenamento local do mantenedor. Somente a chave pública está em `pontelan/update_config.json`. Consulte [Publicar versões](docs/publicar.md).

Ponte é um projeto independente, sem afiliação com Mojang, Microsoft ou Apple. Consulte os avisos das bibliotecas distribuídos junto às releases.
