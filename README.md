# Arquivo corporativo de e-mails

Sistema para arquivar aproximadamente 50 contas Locaweb em um servidor Ubuntu 24.04 ou 26.04 na AWS. Painel em português, acessível somente aos administradores.

## Comportamento

- Coleta incremental por IMAP/TLS a cada cinco minutos, sem sobrepor execuções.
- Preserva mensagens originais e anexos em dois buckets S3 privados e versionados.
- Confere o conteúdo escrito nos dois buckets antes de registrar o arquivamento.
- Pesquisa por conta, assunto, remetente, destinatário, conteúdo e período.
- Download de EML e anexos; auditoria de acessos e operações.
- Senhas IMAP criptografadas e sessões administrativas protegidas.
- Limpeza diária de mensagens com mais de dois meses de calendário, desativada por padrão e liberada por conta após o piloto.
- Revalida ambas as cópias, conteúdo, data e UID antes de excluir. Exige UIDPLUS para exclusão seletiva.
- Manifestos independentes permitem reconstruir o índice do histórico após perda do banco.

## Instalação

Seguir [INSTALL.md](INSTALL.md). O instalador prepara dependências e serviços; não os inicia e não ativa exclusão.

Começar com duas contas, conferir anexos, pesquisa e recuperação, depois expandir para 50. O intervalo de cinco minutos é a meta do serviço; carga inicial e limites do provedor podem alongar os ciclos. A conexão real da Locaweb e a infraestrutura AWS ainda precisam ser validadas pelo responsável pela implantação.

## Componentes

Python/FastAPI · PostgreSQL · S3 · IMAPClient · Nginx · systemd

- `email_archive/web.py`: painel e controle de acesso.
- `email_archive/worker.py`: coleta e limpeza.
- `email_archive/archive.py`: armazenamento e verificação.
- `email_archive/cli.py`: administração e recuperação.
- `deploy/`: instalação, serviços, proxy e exemplo IAM.
- `tests/`: cenários de backup, exclusão segura e acesso administrativo.

## Limites da primeira versão

Somente senha administrativa; usar VPN com MFA ou autenticação adicional no proxy conforme a política corporativa. Alertas aparecem no painel e nos logs; integrar notificações ao monitoramento da empresa. Não há OCR/pesquisa dentro de anexos, captura de mensagens apagadas antes da coleta, nem migração automática de esquema. O prazo de retenção do histórico ainda deve ser definido pela empresa.

## Referências

- [Configuração IMAP da Locaweb](https://www.locaweb.com.br/ajuda/wiki/configuracao-de-outlook-email-locaweb/)
- [Versionamento S3](https://docs.aws.amazon.com/AmazonS3/latest/userguide/Versioning.html)
- [Criptografia S3](https://docs.aws.amazon.com/AmazonS3/latest/userguide/UsingEncryption.html)
