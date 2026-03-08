# Security Policy

## Supported Versions

Only the latest released version of `telegram-sendmail` receives security fixes.
Operators are advised to update to the current release promptly.

| Version | Supported |
|---------|-----------|
| 1.0.x   | ✅ Yes    |

## Threat Model

`telegram-sendmail` operates in a sensitive position on the host: it runs with
the privileges of system daemons such as cron, handles raw email content from
those daemons, reads a config file that contains a Telegram Bot API token, and
makes outbound HTTPS connections to `api.telegram.org`. The following are
considered in-scope security concerns:

- **Token exposure** — the Bot API token in the config file grants full control
  over the configured bot. Misconfigured file permissions (`0640`, `0644`, etc.)
  expose the token to unprivileged local users. The token is automatically
  redacted from all log output at every log level, preventing exposure via
  syslog or console.
- **Privilege escalation** — any path-traversal, arbitrary file-write, or
  command-injection vulnerability reachable via the MIME or SMTP parsing
  pipeline would be in-scope.
- **TOCTOU races** — the spool file is written to directories that may be
  world-writable (`/tmp`). Spool files are protected against symlink attacks
  and unauthorized access regardless of directory permissions. Any regression
  in this area is a security issue.
- **HTML injection into Telegram** — malicious content in an email that
  bypasses the two-pass HTML sanitiser and injects unintended Telegram markup
  is considered a security defect.
- **Dependency vulnerabilities** — vulnerabilities in `requests` or `html2text`
  that affect this tool's attack surface.

The following are explicitly **out of scope**:

- Issues that require the attacker to already have write access to the config
  file or the spool directory.
- Telegram platform security (report those to Telegram directly).
- Issues requiring physical access to the host.

## Reporting a Vulnerability

**Do not open a public GitHub issue for security vulnerabilities.** Public
disclosure before a fix is available puts all deployments at risk.

Vulnerabilities should be reported privately using one of the following methods:

1. **GitHub Private Vulnerability Reporting** (preferred): navigate to the
   [Security tab](https://github.com/theodiv/telegram-sendmail/security/advisories/new)
   of this repository and submit a draft advisory. GitHub keeps the report
   confidential until a coordinated disclosure date is agreed upon.

2. **Direct email**: contact the maintainer via the email address listed on
   the [GitHub profile](https://github.com/theodiv). Encrypt the report with
   the PGP key published there if the content is sensitive.

## Disclosure Process

Upon receipt of a valid report, the maintainer will:

1. Acknowledge receipt within **72 hours**.
2. Assess the severity and, where applicable, assign a CVSS score.
3. Develop and test a fix in a private branch.
4. Coordinate a disclosure timeline with the reporter (target: **14 days**
   from confirmation; complex issues may require longer).
5. Publish a patched release and a GitHub Security Advisory simultaneously.
6. Credit the reporter in the advisory and the `CHANGELOG.md` entry unless
   anonymity is requested.

## Security Hardening Recommendations

The following deployment practices are recommended for operators running
`telegram-sendmail` in production:

- **Config file permissions**: the config file must be owned by the user
  running the daemon and have `0600` permissions. The application warns but
  does not refuse to run on loose permissions.
   ```bash
   chmod 600 /etc/telegram-sendmail.ini
   chown root:root /etc/telegram-sendmail.ini
   ```
- **Spool directory**: prefer a dedicated, non-world-writable spool directory
  over the `/tmp` fallback. Set `spool_dir` in `[options]` accordingly.
- **Bot token scope**: create a dedicated bot for each host or environment.
  A compromised token can be revoked via @BotFather without affecting other
  deployments.
- **Network egress filtering**: the only outbound connection required is HTTPS
  to `api.telegram.org` (TCP/443). Restricting all other egress with a host
  firewall reduces the blast radius of any future supply-chain compromise in
  the dependency tree.
- **Log monitoring**: monitor `journalctl -t telegram-sendmail` for repeated
  `WARNING` or `ERROR` entries, particularly permission warnings on the config
  file and spool directory.
