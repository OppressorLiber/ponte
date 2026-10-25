# Ponte

**Seu mundo. Seus amigos.** Um aplicativo para Windows que compartilha a conexão de um mundo do Minecraft Java com amigos pela internet.

## Baixar e jogar

Baixe **Ponte.exe** na [versão mais recente](https://github.com/OppressorLiber/ponte/releases/latest). O aplicativo é portátil: não precisa instalar Python ou um driver de rede.

1. O anfitrião abre seu mundo e escolhe **Abrir para LAN** no Minecraft.
2. No Ponte, informa a porta exibida no chat e cria um convite.
3. O amigo abre o Ponte, informa seu nome e pede entrada com o convite.
4. O anfitrião aprova a primeira solicitação e pode confiar naquele computador para liberar as próximas entradas automaticamente.
5. O amigo copia o endereço local e usa **Multijogador → Conexão direta → colar endereço** no Minecraft.

Mantenha o jogo e o Ponte abertos durante a partida. Os jogadores precisam usar versões e modpacks compatíveis entre si. O túnel transporta uma porta TCP do jogo; mods que usam portas adicionais podem exigir suporte próprio. Esta ferramenta não altera saves, mods, launcher ou contas.

## Controle da sala — 0.2.4

O anfitrião aprova ou recusa solicitações antes de liberar o túnel para o mundo. Ao aprovar, pode marcar **Confiar neste computador**; também pode confiar em alguém que já está conectado. Computadores de confiança entram automaticamente nas próximas salas quando apresentam um convite válido. A lista fica salva no computador do anfitrião e pode ser gerenciada e revogada.

A confiança é verificada por uma chave Ed25519 persistente do computador do convidado, que assina a oferta vinculada ao convite, à tentativa e ao SDP. Copiar o nome ou ter o convite não permite se passar por um computador já confiável. Nomes continuam sendo informados pelos participantes e não substituem a identidade da conta Minecraft. Solicitações desconhecidas expiram após dois minutos.

**Remover** encerra a conexão daquela pessoa e retira a confiança do computador. Para voltar, ela precisa pedir entrada e receber uma nova aprovação. **Renovar convite** invalida o convite anterior para novas entradas e mantém os participantes já conectados; solicitações e negociações ainda incompletas são canceladas. Se o serviço não conseguir preparar o novo convite, o anterior continua funcionando.

Atualize os dois PCs para receber os novos estados de espera. Veja [Controle da sala](docs/salas.md). A integração automática por mod Forge foi estudada e está descrita em [Mod Forge](docs/mod-forge.md); o mod ainda não faz parte desta versão.

## Atualizações

A partir da versão **0.2.0**, o Ponte verifica automaticamente o GitHub ao iniciar e a cada seis horas. Quando a sessão está encerrada, baixa a nova versão em segundo plano e oferece **Reiniciar e atualizar**. A partida ativa nunca é encerrada para instalar uma atualização.

O arquivo recebido precisa corresponder a um manifesto assinado com Ed25519, ao tamanho informado e ao SHA-256. Um processo separado troca o executável, preserva preferências e confirma que a nova versão iniciou; se a inicialização falhar, restaura a versão anterior. Não há token do GitHub dentro do aplicativo.

Quem ainda usa **0.1.0** precisa baixar a versão mais recente uma vez, pois aquela primeira versão não contém o atualizador.

### Indicador de latência

A versão **0.2.1** usa relógio de alta precisão. Valores menores que um milissegundo aparecem como `<1 ms`; amostras antigas deixam de ser apresentadas como atuais. O número mede a ida e volta do aplicativo, incluindo filas e processamento. Uma medição isolada não identifica a causa de um pico. Passe o mouse sobre a latência de um participante para ver mediana e p95; o diagnóstico inclui o histórico recente, timeouts de sondas e atraso do loop local. Timeout de uma sonda não representa uma porcentagem de perda de pacotes da rede.

### Menos fila antes do envio — 0.2.3

O envio dos dados agora aguarda espaço quando os buffers locais dos DataChannels estão ocupados. O limite de admissão é 16 KiB compartilhado entre as conexões; dados já entregues ao SCTP continuam usando seu controle de congestionamento e a janela adaptativa de créditos da 0.2.2. Isso evita acumular uma fila grande antes de o transporte conseguir transmiti-la. Não é um limite fixo de velocidade em Mbit/s.

Conexões canceladas continuam contabilizadas enquanto seus dados pendentes drenam. A medição de confirmação começa no envio efetivo. Encerrar a sala também cancela os transportes TCP ativos ou pausados, evitando esperar indefinidamente por um cliente. O encerramento normal de cada fluxo mantém os bytes e o half-close.

Comparação com o transporte publicado da **0.2.2**, congelado no teste, usando TCP/ICE/DTLS/SCTP reais e transferências simultâneas de 1 MB por direção:

| Caminho simulado | p95 0.2.2 → 0.2.3 | Upload | Download |
|---|---:|---:|---:|
| 2 Mbit/s por sentido, RTT 40 ms | 944 → 386 ms | +5,5% | +3,5% |
| 20 Mbit/s por sentido, RTT 100 ms | 846 → 565 ms | +22,7% | +19,0% |

São médias dos resultados de duas repetições por cenário, com ordem invertida. Os oito braços passaram: 16 MB conferidos por SHA-256 e 390 respostas TCP íntegras, sem timeouts. As amostras de fila local caíram de aproximadamente 119–123 KiB para 12–13 KiB. O limite de admissão considera os buffers públicos; mensagens de controle, créditos, FIN e dados internos/em trânsito no SCTP têm contabilização própria.

Testes adicionais de 4 MB por direção verificaram integridade. A carga do PC variou nesses ensaios, com atraso do loop chegando a 171 ms. Uma checagem adicional em processos novos, com atraso de loop abaixo de 15 ms, mediu upload de 6,41 → 7,38 Mbit/s e download de 6,63 → 7,59 Mbit/s; é apenas um par de execuções e não uma promessa de ganho contínuo. Num ensaio adicional com perda simulada de 0,3% e atraso extra uniforme de 0–5 ms por datagrama, a candidata completou os dois cenários com hashes e sondas corretos. A referência atingiu o limite de 30 s com upload incompleto em ambos; não calculamos melhora percentual contra esses braços incompletos. Esse modelo pode reordenar datagramas e usa uma única semente.

Todos esses ensaios são no mesmo PC, com um caminho moldado. O ganho entre as redes dos jogadores ainda precisa ser confirmado. Os relatórios brutos e os limites das medições acompanham a release.

Para reproduzir a comparação principal: `python qa_transport.py --baseline 0.2.2 --repetitions 2 --bulk-bytes 1000000 --max-seconds 20 --output qa-comparacao.json`. A candidata usa os valores de produção; `--candidate-window` é uma opção exploratória, não necessária para reproduzir a versão publicada.

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
