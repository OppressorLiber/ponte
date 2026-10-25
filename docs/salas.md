# Controle da sala

## Aprovar entradas

No computador do amigo, informe um nome e entre com o convite. O Ponte aguarda a decisão do anfitrião antes de apresentar o endereço para o Minecraft.

Na sala do anfitrião, cada pedido mostra o nome informado e um identificador curto. Use **Aprovar** ou **Recusar**. Se quiser liberar as próximas entradas daquele computador, marque **Confiar neste computador** ao aprovar. O nome é uma identificação para a solicitação, não uma verificação da conta Minecraft. Pedidos sem decisão expiram após dois minutos. A sala aceita até oito amigos conectados e mantém uma quantidade limitada de pedidos em espera.

Somente após a aprovação o anfitrião negocia a conexão WebRTC com o convidado. Não é aberto acesso ao mundo enquanto a solicitação está pendente. A aprovação não altera whitelist, permissões ou autenticação do Minecraft.

## Computadores de confiança

Depois da primeira aprovação com confiança, aquele computador pode entrar automaticamente usando o convite válido de uma sala sua. A confiança também pode ser concedida a um participante já conectado. A lista permanece salva entre salas e reinicializações do Ponte.

Cada computador convidado possui uma chave Ed25519 local. Sua assinatura vincula o pedido ao convite atual e à tentativa de conexão. O anfitrião guarda a chave pública autorizada, e não uma senha do Minecraft. Copiar o mesmo nome, endereço IP ou convite não substitui essa assinatura.

Use **Computadores de confiança** para ver a lista e retirar uma autorização, mesmo quando a pessoa estiver offline. Retirar a confiança mantém a partida atual; a próxima entrada volta a exigir aprovação. Se o convidado apagar sua chave local, ele será reconhecido como um novo computador e precisará de outra aprovação. Não compartilhe o arquivo de chave privada com outras pessoas.

## Remover alguém

Use **Remover** na linha do participante. O túnel dessa pessoa é encerrado, incluindo os fluxos TCP abertos, e a confiança de seu computador é retirada. As conexões dos demais participantes continuam ativas. O removido pode pedir entrada novamente, mas depende de outra aprovação; a ação não é um banimento permanente de uma conta Minecraft.

## Renovar o convite

Use **Renovar convite** quando quiser invalidar o convite que já compartilhou. Copie e envie o novo convite às próximas pessoas que entrarão.

- Quem já está conectado permanece na partida.
- Pedidos pendentes e conexões ainda em negociação são cancelados.
- O convite anterior deixa de receber novas entradas.
- Se não for possível preparar o novo convite, o atual é preservado.

Para retirar alguém que já entrou, use também **Remover**. Renovar o convite controla entradas futuras e não desconecta os participantes ativos.

O aplicativo mantém convites em memória e os copia para a área de transferência somente por ação do usuário. Não os inclua em diagnósticos ou capturas públicas.

## Compatibilidade

Atualize anfitrião e convidados para a versão 0.2.4 ou posterior. O formato de transporte TCP permanece o mesmo; convidados antigos podem encerrar a tentativa antes do prazo de aprovação, pois não conhecem o novo estado de espera.

As verificações automatizadas usam TCP e WebRTC reais no mesmo computador. Elas não substituem a validação da partida entre as redes dos jogadores.
