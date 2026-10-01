# Arquivamento de e-mails Locaweb na AWS

## Estado do projeto

Este documento define os requisitos e o roteiro de implantação. A aplicação ainda precisa ser implementada. Os comandos abaixo preparam o servidor; não instalam um sistema pronto nem ativam exclusões.

## Regras aprovadas

- Ubuntu 24.04 na AWS, aproximadamente 50 contas Locaweb.
- Coleta incremental a cada cinco minutos, incluindo recebidos, enviados e demais pastas IMAP.
- Salvar mensagens originais, cabeçalhos e anexos sem duplicação visível.
- Não excluir mensagens durante a coleta.
- Limpar somente mensagens com mais de dois meses de calendário, desde a data de recebimento registrada pelo servidor IMAP (INTERNALDATE). Validar a referência de data dos enviados no piloto.
- Excluir somente após verificar integridade do arquivo, registro no banco e confirmação da cópia de segurança independente.
- Em falha ou dúvida, manter a mensagem e registrar alerta.
- Histórico acessível exclusivamente aos administradores.
- Prazo de retenção do histórico pendente: não configurar expiração automática até sua definição.

## Arquitetura prevista

- Serviço de coleta e painel administrativo no servidor Linux.
- PostgreSQL para índice de pesquisa, contas, estado da coleta e auditoria.
- S3 privado para arquivos EML, com criptografia e versionamento.
- Backup do banco e cópia independente dos arquivos com permissões separadas. Versionamento não substitui backup.
- Proxy HTTPS e acesso ao painel por VPN ou rede administrativa restrita.
- Perfil IAM da instância com permissões mínimas; nenhuma chave AWS no código.
- Credenciais IMAP em armazenamento de segredos, nunca no Git ou nos logs.

## Preparação do Ubuntu

Executar no servidor autorizado:

```bash
cat /etc/os-release
sudo apt-get update
sudo apt-get install -y ca-certificates curl git python3 python3-venv postgresql nginx
sudo adduser --system --group --home /opt/email-archive email-archive
sudo install -d -o email-archive -g email-archive -m 0750 /opt/email-archive/app
sudo install -d -o root -g email-archive -m 0750 /etc/email-archive
```

Confirmar acesso administrativo funcional antes de alterar SSH, firewall ou grupos de segurança. Restringir SSH à rede administrativa e não expor PostgreSQL à internet. Usar HTTPS no painel.

## Configuração da AWS

1. Criar bucket exclusivo, privado e com bloqueio de acesso público.
2. Habilitar criptografia e versionamento.
3. Configurar perfil IAM da instância limitado aos recursos necessários. O coletor não deve poder apagar permanentemente versões do arquivo.
4. Configurar cópia independente e registrar sua confirmação por mensagem ou lote verificável.
5. Configurar backup do PostgreSQL e testar restauração.
6. Configurar alertas de coleta atrasada, falha de backup, indisponibilidade e falta de espaço.

Não mover mensagens para classes que exigem restauração demorada enquanto a consulta imediata for requisito. Dimensionar servidor e armazenamento conforme volume acumulado e crescimento mensal.

## Configuração da Locaweb

- Confirmar produto contratado, hostname IMAP e método de acesso de cada conta.
- Usar IMAP com SSL/TLS na porta 993 e validação do certificado.
- Validar autenticação de contas com segundo fator conforme as opções do plano.
- Testar leitura integral e listagem de todas as pastas.
- Não usar POP para a coleta.

Não presumir que a senha administrativa permite ler todas as caixas. Provisionar credenciais por canal seguro.

## Requisitos do coletor

- Agendar a cada cinco minutos e impedir execuções sobrepostas.
- Limitar conexões simultâneas, respeitar limites do provedor e repetir falhas com atraso progressivo.
- Identificar mensagens por conta, pasta, UIDVALIDITY e UID; verificar integridade por hash criptográfico.
- Reconciliar mudanças de pasta e UIDVALIDITY sem perder mensagens ou associar exclusões ao item errado.
- Gravar o arquivo, verificar checksum e persistir o estado antes de marcar como arquivado.
- Não interpretar falha ou resposta incompleta como caixa vazia.
- Indexar conteúdo e permitir download do EML original.

IMAP não garante capturar mensagens apagadas pelo usuário antes da próxima coleta. Se for necessário preservar toda mensagem que passa pelo servidor, avaliar captura no fluxo de entrega com a Locaweb.

## Requisitos da limpeza

Tarefa separada, inicialmente desativada; pode executar diariamente.

1. Selecionar mensagens anteriores ao corte de dois meses de calendário, calculado em UTC.
2. Confirmar arquivo íntegro, registro persistido e cópia independente disponível.
3. Confirmar novamente identidade e idade da mensagem no IMAP.
4. Registrar operação e remover exclusivamente mensagens elegíveis.
5. Evitar EXPUNGE genérico, que pode apagar mensagens marcadas por outros clientes. Validar suporte a exclusão seletiva (UID EXPUNGE/UIDPLUS). Sem mecanismo seguro, suspender a limpeza da conta.
6. Em falha ou dúvida, manter a mensagem e emitir alerta.

A exclusão no servidor será refletida nos clientes IMAP. Remover da caixa nunca deve remover o arquivo histórico.

## Painel administrativo

- Login individual de administradores, preferencialmente com autenticação multifator.
- Busca por conta, remetente, destinatário, assunto, período e texto.
- Sanitização do HTML e bloqueio de conteúdo remoto automático.
- Download autorizado de mensagens e anexos.
- Auditoria de consultas, downloads, alterações e limpeza.
- Exibir última coleta, último backup, volume arquivado, falhas e mensagens elegíveis.

## Implantação por etapas

1. Implementar a aplicação e os serviços de execução, com limpeza desativada por padrão.
2. Cadastrar duas contas de piloto por canal seguro.
3. Conferir pastas, contagens, mensagens antigas e recentes, enviados e anexos.
4. Testar busca, download e recuperação real a partir do backup.
5. Simular limpeza e revisar a lista elegível.
6. Validar data dos enviados e exclusão seletiva.
7. Ativar limpeza no piloto e confirmar preservação das mensagens recentes.
8. Expandir gradualmente para 50 contas, acompanhando duração e limites de conexão.

## Critérios de aceite

- Nenhuma exclusão antes de arquivo íntegro e backup confirmado.
- Mensagens recentes permanecem na Locaweb.
- Mensagens removidas continuam pesquisáveis e recuperáveis.
- Repetir coleta não cria duplicatas visíveis.
- Falhas de rede e backup bloqueiam operações inseguras e geram alertas.
- Somente administradores autorizados acessam o histórico.
- Restauração de arquivos e banco demonstrada.

## Pendências para implantação

- Endereço administrativo do servidor, domínio HTTPS e rede de acesso.
- Região AWS, volume acumulado e crescimento mensal.
- Hostname IMAP e provisionamento seguro das credenciais.
- Prazo de guarda do histórico e responsáveis pelo acesso.

## Referências

- [Locaweb: configuração IMAP](https://www.locaweb.com.br/ajuda/wiki/configuracao-de-outlook-email-locaweb/)
- [Locaweb: backup de mensagens](https://www.locaweb.com.br/ajuda/wiki/como-realizar-o-backup-de-mensagens-email-locaweb/)
- [AWS: versionamento S3](https://docs.aws.amazon.com/AmazonS3/latest/userguide/Versioning.html)
- [AWS: criptografia S3](https://docs.aws.amazon.com/AmazonS3/latest/userguide/UsingEncryption.html)
