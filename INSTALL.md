# Instalação no Ubuntu 24.04 / AWS

Esta versão está pronta para um piloto controlado. Os testes locais usam IMAP/S3 simulados; o ambiente real da Locaweb e da AWS precisa ser validado antes de habilitar qualquer exclusão. Não substitua serviços já existentes no servidor.

## 1. Preparar o servidor

O instalador usa Python, PostgreSQL, Nginx e systemd. Não requer Docker.

```bash
sudo apt-get update
sudo apt-get install -y git
sudo mkdir -p /opt/email-archive
sudo git clone --branch sistema-email https://github.com/marcooliveira-dot/git-servidor-de-email-.git /opt/email-archive/app
cd /opt/email-archive/app
sudo bash deploy/install.sh
```

O instalador cria o usuário de serviço, ambiente Python, configuração com segredos aleatórios e os serviços systemd. **Não inicia o coletor e não ativa limpeza.** Se o repositório já estiver nessa pasta, use `sudo git pull --ff-only` em vez de cloná-lo novamente. Confirme a branch que contém a aplicação.

## 2. Criar o banco

```bash
sudo -u postgres psql
```

Dentro do PostgreSQL:

```sql
CREATE ROLE email_archive LOGIN;
\password email_archive
CREATE DATABASE email_archive OWNER email_archive;
\q
```

Use uma senha aleatória exclusiva. `\password` evita incluí-la em um comando SQL no histórico. Não publique a senha. PostgreSQL deve ouvir apenas na interface local ou em rede privada restrita.

## 3. Preparar os buckets e o perfil IAM

- Criar dois buckets privados diferentes: arquivo principal e cópia independente.
- Habilitar bloqueio de acesso público, criptografia SSE-S3 e versionamento em ambos.
- Não configurar expiração de objetos ou versões até definir a retenção corporativa.
- Anexar à instância um perfil IAM com a política de `deploy/iam-policy.example.json`, substituindo os nomes. O sistema não requer permissão de apagar arquivos do S3.
- Restringir a administração/exclusão do backup a responsáveis separados; preferir conta AWS separada para proteção contra comprometimento da conta principal. Dois buckets com os mesmos administradores não oferecem isolamento completo contra esse risco.
- Os EMLs e seus manifestos JSON são escritos e lidos de volta nos dois buckets. Os manifestos permitem reconstruir o índice se o banco for perdido.

## 4. Editar a configuração

```bash
sudo nano /etc/email-archive/archive.env
```

Preencher:

```text
DATABASE_URL="postgresql+psycopg://email_archive:SENHA_CODIFICADA@127.0.0.1/email_archive"
PRIMARY_BUCKET="nome-do-bucket-principal"
BACKUP_BUCKET="nome-do-bucket-backup"
AWS_REGION="sa-east-1"
BASE_URL="https://arquivo.seudominio.com.br"
COOKIE_SECURE="true"
CLEANUP_ENABLED="false"
POLL_SECONDS="300"
```

Preservar `ENCRYPTION_KEY` e `SESSION_SECRET` gerados pelo instalador. Codificar caracteres especiais da senha da URL com percent-encoding. Não enviar o arquivo de configuração por chat, e-mail ou Git.

`BASE_URL` deve corresponder à origem exata usada pelos administradores, incluindo porta se houver. Não inclui caminhos. O arquivo deve ser de propriedade de root, com permissão 600.

```bash
sudo chmod 600 /etc/email-archive/archive.env
sudo email-archive-manage init-db
sudo email-archive-manage admin administrador
sudo email-archive-manage check-storage
```

A senha administrativa é solicitada de forma oculta; mínimo 14 caracteres. Repita o comando `admin` para criar outros administradores ou trocar a senha e invalidar sessões antigas.

## 5. Habilitar acesso HTTPS restrito

Usar `deploy/nginx.conf.example` como modelo de um virtual host dedicado. Ajustar domínio, certificados válidos e rede VPN antes de habilitá-lo. Não sobrescrever configurações existentes. O aplicativo ouve apenas em `127.0.0.1:8000`.

```bash
sudo nginx -t
sudo systemctl reload nginx
sudo systemctl enable --now email-archive-web
```

Restringir grupos de segurança da AWS: SSH apenas da rede administrativa; HTTPS apenas da VPN/rede autorizada. Não expor a porta 8000 ou 5432. Não alterar regras de acesso antes de confirmar um caminho administrativo funcional. Esta versão usa senha e sessão administrativa; MFA ainda não está implementado. Usar VPN com MFA ou autenticação adicional no proxy conforme política da empresa.

## 6. Cadastrar duas contas piloto

Abrir o endereço HTTPS configurado, entrar e acessar **Contas**:

1. Informar e-mail/usuário IMAP, hostname confirmado na Locaweb e senha da conta.
2. Repetir para a segunda conta.
3. Confirmar com a Locaweb o método de autenticação para caixas com segundo fator. A senha do painel administrativo pode não dar acesso às caixas.

As senhas das contas são criptografadas com Fernet. O acesso ao servidor e à chave permite descriptografá-las; restrinja essas permissões e preserve a chave em backup seguro separado.

```bash
sudo systemctl enable --now email-archive-worker
sudo systemctl status email-archive-web email-archive-worker
sudo journalctl -u email-archive-worker --since '30 minutes ago'
```

O serviço tenta iniciar uma coleta a cada 300 segundos. Não sobrepõe execuções. A carga inicial pode demorar mais do que cinco minutos, dependendo de mensagens, anexos e limites do provedor. Nesse caso registra atraso e inicia o próximo ciclo assim que possível. As 50 contas são processadas sequencialmente nesta versão. Medir a duração no piloto e no aumento gradual; o intervalo é uma meta operacional, não uma garantia durante a carga inicial.

É possível fazer coleta única, sem excluir:

```bash
sudo email-archive-manage collect-once
```

Se o worker estiver ativo, o bloqueio impedirá esse comando de executar em paralelo.

## 7. Validar antes de expandir

- Conferir recebidos, enviados, subpastas e anexos.
- Conferir mensagens recentes e antigas.
- Buscar por conta, conteúdo, remetente e período.
- Baixar um EML e abrir em cliente de e-mail.
- Baixar anexos e conferir seu conteúdo.
- Confirmar objetos versionados nos dois buckets.
- Simular falha do backup no ambiente piloto e confirmar que nenhuma exclusão ocorre.
- Verificar a data INTERNALDATE dos enviados. Mensagens importadas podem ter data de recebimento no servidor diferente do cabeçalho Date; a regra usa INTERNALDATE.
- Testar UIDPLUS/exclusão seletiva da Locaweb antes da limpeza. Se não suportado, manter limpeza bloqueada.

IMAP pode não capturar mensagens que o usuário apague antes da próxima coleta. Se for necessário preservar todo e-mail desde a entrega, avaliar captura no fluxo de entrega com o provedor.

## 8. Fazer backup do banco e da configuração

Configurar backup regular do PostgreSQL, armazenamento privado e teste real de restauração. Exemplo de geração local, executado por um operador autorizado:

```bash
sudo install -d -o postgres -g postgres -m 0700 /var/lib/postgresql/email-archive-backups
sudo -u postgres bash -c 'umask 077; pg_dump -Fc email_archive > /var/lib/postgresql/email-archive-backups/email-archive.dump'
```

O exemplo sobrescreve o arquivo local: em produção usar nomes por data, retenção definida e cópia protegida fora da instância. O dump contém dados de mensagens e credenciais IMAP criptografadas. Guardar `archive.env` em backup criptografado e separado; não junto a arquivos públicos. Automatizar esse procedimento pela rotina corporativa de backup.

Para recuperar após perda do banco: restaurar o dump com `pg_restore` em banco vazio, preservar a chave original e criar/redefinir administradores. Para reconstruir mensagens posteriores ao dump usando os manifestos:

```bash
sudo systemctl stop email-archive-worker
sudo email-archive-manage restore-index
```

A recuperação verifica as duas cópias de cada EML. Pausa as contas recuperadas e desativa sua limpeza. Não recupera a auditoria nem senhas de contas que não estavam no dump. Cadastre/atualize as credenciais e revise antes de retomar coleta. Recriar banco vazio exige `init-db` e `admin` antes de usar o painel. Realizar esse teste em ambiente isolado antes do piloto de limpeza.

## 9. Simular e liberar limpeza após o piloto

A tela **Limpeza** e o comando abaixo apenas mostram candidatas por idade; não apagam nada:

```bash
sudo email-archive-manage cleanup-report
```

A regra usa dois meses de calendário, em UTC, a partir de INTERNALDATE. Mensagens exatamente no limite são mantidas até ultrapassá-lo. A tarefa de limpeza é separada da coleta e executa, quando habilitada, uma vez por dia. Só mensagens já arquivadas são candidatas.

Após teste de recuperação e revisão explícita, liberar uma conta piloto:

```bash
sudo email-archive-manage account-cleanup colaborador@seudominio.com.br --enable
```

O comando pede que o operador digite o endereço da conta. Em seguida alterar `CLEANUP_ENABLED="true"` em `archive.env` e reiniciar o worker:

```bash
sudo systemctl restart email-archive-worker
```

**A limpeza pode executar já no primeiro ciclo após o reinício.** Cada mensagem exige leitura íntegra das duas cópias, UIDVALIDITY consistente, conteúdo e data correspondentes no IMAP e suporte UIDPLUS. A exclusão na Locaweb também se reflete nos clientes IMAP. O histórico no S3 permanece.

Para suspender imediatamente a rotina, parar o worker; depois desativar a flag global antes de retomá-lo. Para retirar a autorização de uma conta:

```bash
sudo email-archive-manage account-cleanup colaborador@seudominio.com.br
```

## 10. Operação e atualizações

- Monitorar serviços systemd, `/healthz`, contas com erro e coleta atrasada no painel. `/healthz` verifica o painel/banco, não a disponibilidade de cada caixa ou dos buckets. A versão não envia alertas por e-mail; integrar monitoramento da empresa.
- Expandir de duas para 50 contas gradualmente, medindo tempo, consumo e armazenamento.
- Fazer backup e parar serviços antes de atualizar código. Usar `git pull --ff-only`, instalar dependências testadas, iniciar serviços e validar o piloto.
- Esta é a primeira versão do esquema; `init-db` não migra tabelas existentes. Mudanças futuras exigem migrações explícitas.
- Evitar registrar URLs com termos de busca no proxy e não copiar logs que contenham dados de mensagens.
- Login administrativo expira após oito horas; há limite persistente de tentativas. Não há acesso para colaboradores.

## Verificação de desenvolvimento

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-lock.txt
.venv/bin/python -m pytest -q
bash -n deploy/install.sh deploy/manage
```

Não executar testes com credenciais reais. A suíte usa serviços simulados e banco SQLite temporário; produção usa PostgreSQL.

`requirements-lock.txt` registra o conjunto de versões verificado, incluindo ferramentas de teste. Atualizar o lock somente após testar o novo conjunto.
