# Integração Forge: viabilidade e próximo escopo

Este documento descreve uma integração possível. O Ponte 0.2.4 não inclui um mod nem instala arquivos no Minecraft.

## Fluxo pretendido

1. O anfitrião usa **Abrir para LAN** normalmente.
2. O mod observa que o servidor integrado foi publicado e lê sua porta real.
3. Um motor do Ponte sem janela cria a sala e retorna um convite ao jogo.
4. O amigo cola o convite numa entrada do mod. Também é possível implementar um endereço-convite reconhecido pelo mod em **Adicionar servidor**.
5. Após a aprovação do anfitrião, os motores negociam ICE/STUN e abrem o caminho direto.
6. O motor do amigo abre um proxy TCP local; o Minecraft conecta automaticamente a esse endereço.

O amigo também precisa do mod ou do Ponte. O `127.0.0.1:porta` do anfitrião não é um endereço compartilhável entre PCs. O endereço externo descoberto por STUN é parte do WebRTC/UDP, não um servidor TCP que o Minecraft comum saiba usar. Um endereço-convite exige que o mod intercepte e resolva a entrada antes de encaminhar a conexão ao proxy local.

## Implementação recomendada

Começar com um mod fino e um motor do Ponte sem janela, controlado por IPC local autenticado e restrito ao usuário. A interface do jogo gerencia o ciclo do mundo, pedidos de entrada e conexão; o motor reutiliza `TunnelService.host`, `join`, `approve`, `reject`, `remove_peer` e `renew_invite`.

O modo sem janela, o protocolo IPC e sua integração ainda precisam ser implementados. Operações de rede precisam ficar fora das threads do jogo; fechar o mundo deve encerrar sua sala e suas conexões.

Na linha Forge 1.20.1, é possível observar `TickEvent.ClientTickEvent` e a transição de `IntegratedServer.isPublished()`, lendo `getPort()`. Isso acompanha a publicação bem-sucedida, sem depender do texto traduzido do chat. `ScreenEvent.Init.Post` permite adicionar controles às telas. `ServerStartedEvent` ocorre antes da abertura para LAN e, sozinho, não representa o momento correto.

Fontes primárias: [TickEvent do Forge 1.20.1](https://github.com/MinecraftForge/MinecraftForge/blob/1.20.1/src/main/java/net/minecraftforge/event/TickEvent.java), [ScreenEvent do Forge 1.20.1](https://github.com/MinecraftForge/MinecraftForge/blob/1.20.1/src/main/java/net/minecraftforge/client/event/ScreenEvent.java) e [mapeamentos oficiais Mojang 1.20.1](https://piston-data.mojang.com/v1/objects/6c48521eed01fe2e8ecdadbd5ae348415f3c47da/client.txt).

Uma alternativa é incorporar uma biblioteca WebRTC Java/JNI no mod. [webrtc-java](https://github.com/devopvoid/webrtc-java) oferece DataChannels e binários nativos, mas exige implementar interoperabilidade com os frames e créditos próprios do Ponte, empacotar dependências e validar seu ciclo de vida. Essa alternativa é um projeto maior.

## Limites

- Cada linha Minecraft/Forge exige compilação e testes próprios. O motor de transporte pode ser comum, mas um único JAR universal não é uma premissa segura.
- O mod não converte versões ou modpacks incompatíveis e não adiciona suporte automático a mods que usam UDP ou portas extras.
- ICE/STUN sem TURN continua dependendo do comportamento dos NATs e firewalls. CGNAT não implica falha automática, mas algumas combinações impedem o caminho direto. Veja a motivação de retransmissão no [RFC 8656](https://www.rfc-editor.org/rfc/rfc8656.html).
- A primeira validação deve usar uma versão e um modpack definidos, dois clientes reais e depois duas redes externas.
