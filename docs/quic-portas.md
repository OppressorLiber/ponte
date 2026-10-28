# QUIC e portas adicionais

O Ponte continua sendo um aplicativo externo para Windows. A conexão principal do Minecraft Java permanece TCP; o aplicativo encaminha seus bytes por um transporte QUIC direto entre os computadores.

## Transporte

QUIC usa TLS 1.3 e o caminho UDP selecionado pelo ICE/STUN. Não passa pelo SCTP ou por uma conexão TCP intermediária. Não há servidor de retransmissão da partida. Os certificados efêmeros dos dois participantes são vinculados à negociação autenticada; o certificado do convidado também fica vinculado à assinatura de identidade usada pela confiança persistente.

A implementação usa `aioquic`, com sua API QUIC independente de sockets, e `aioice` para a travessia de NAT. Não inclui a mudança de motor para C++ proposta separadamente. O modo **WebRTC (compatível)** permanece disponível no convidado para conexões com versões anteriores. Não ocorre troca silenciosa de protocolo ao falhar o caminho direto.

O anfitrião pode receber convidados QUIC e WebRTC na mesma sala. As portas adicionais desta versão são disponibilizadas para convidados QUIC. Atualize os dois computadores para usar todas as funções.

Cada conexão TCP usa um stream QUIC confiável e ordenado. A recuperação de perdas e o controle de congestionamento do QUIC substituem o SCTP nesse caminho. O protocolo do jogo continua intacto: o Ponte não separa movimento e chunks que compartilham uma mesma conexão TCP. Trocar o transporte não altera o processamento do servidor Minecraft ou a latência física da operadora.

## Portas adicionais

Antes de criar a sala, abra **Portas adicionais**. Cadastre até oito serviços:

- **Nome:** descrição para o grupo.
- **Protocolo:** TCP para fluxos confiáveis ou UDP para datagramas.
- **Porta anfitrião:** porta do serviço no computador que abriu o mundo.
- **Porta convidado:** porta local que cada amigo usará. Zero permite uma porta livre automática.

Os destinos do anfitrião e os listeners do convidado ficam restritos a `127.0.0.1`. O convidado não pode solicitar um endereço ou uma porta de destino que o anfitrião não cadastrou. Alterar as portas exige encerrar e abrir a sala com a nova configuração.

As portas efetivamente abertas aparecem na sessão do convidado, incluindo TCP/UDP. Se uma porta fixa já estiver ocupada, ajuste a configuração ou encerre o aplicativo que a utiliza; o Ponte não substitui o programa existente.

## UDP e voz

Datagramas são transportados em QUIC DATAGRAM, sem retransmissão da aplicação. Em congestionamento, dados UDP podem ser descartados para evitar entregar áudio antigo. Datagramas grandes são fragmentados e remontados com prazo e memória limitados; perder um fragmento descarta o datagrama inteiro.

O preset **Voz (UDP 24454)** apenas preenche uma porta comum do Simple Voice Chat. Não instala o mod, altera seus arquivos ou garante que a configuração de voz use essa porta no mundo LAN. Confira a porta real, o endereço anunciado e os requisitos da versão do mod. A validação genérica de UDP não equivale à validação de uma conversa no mod.

## Salas e confiança

QUIC e as portas extras respeitam a aprovação antes do acesso ao mundo, a lista de computadores de confiança, a remoção e a renovação do convite. Remover um participante também encerra seus streams TCP e associações UDP. Renovar o convite mantém os participantes já conectados e invalida pedidos ainda incompletos.

## Fontes técnicas

- [API QUIC do aioquic](https://aioquic.readthedocs.io/en/latest/quic.html)
- [API ICE do aioice](https://aioice.readthedocs.io/en/latest/api.html)
- [QUIC e recuperação de perdas](https://www.rfc-editor.org/rfc/rfc9002.html)
- [QUIC DATAGRAM](https://www.rfc-editor.org/rfc/rfc9221.html)
- [Configuração do Simple Voice Chat](https://modrepo.de/minecraft/voicechat/wiki/server_config)
