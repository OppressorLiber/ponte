# Publicar uma versão

1. Ajustar `pontelan.__version__`, `version_info.txt` e o nome de versão no spec. O executável público sempre se chama `Ponte.exe`.
2. Rodar os testes e gerar o executável pelo spec, preservando as correções de DLL/SSL do runtime usado.
3. Executar `release.py --exe CAMINHO --out PASTA --notes-file NOTAS`. Por padrão, o assinador procura a chave privada em `%LOCALAPPDATA%\Ponte\release-signing\ed25519-private.pem`. Outra localização pode ser informada por `--key`. Nunca colocar essa chave no Git ou nos assets.
4. Revisar e fazer commit/push dos fontes. Criar uma release pública estável `vX.Y.Z` no repositório `OppressorLiber/ponte` com `Ponte.exe`, `update.json` e `update.sig`, além dos avisos de dependências quando aplicável.
5. Conferir a API `releases/latest`, a assinatura, o SHA-256 do download e um ciclo real de troca/reinício antes de anunciar a atualização.

O assinador verifica se a chave privada corresponde à chave pública já distribuída. Trocar a chave privada sem um plano de migração impede clientes existentes de aceitar versões novas.
