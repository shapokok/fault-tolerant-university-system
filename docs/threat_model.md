# Threat model (STRIDE)

System: the endterm stack (`docker-compose.ft.yml`, SECURITY_MODE=on). The baseline has none of these
controls on purpose; it is the "before" picture.

Assets: student records and grades, payments and the ledger, user accounts, the audit log, secrets (.env).

"Tested by" letters refer to sections of `experiments/security_check.py`; the latest result is
`results/endterm/e3_security_check.txt` (89 of 89 checks passed, 2026-11-06).

| # | STRIDE | Asset | Threat scenario | Control | Tested by |
|---|---|---|---|---|---|
| 1 | Spoofing | Student account | Attacker guesses a password by trying many | bcrypt hashes (cost 10); lockout after 5 failures for 60 s; login rate limit 5/s per IP (burst 10); AuthFailureSpike alert | G (lockout), H (rate limit) |
| 2 | Spoofing | Student account | Attacker finds valid usernames from the error message | Same answer for an unknown user and a wrong password; a dummy bcrypt check for unknown users, so both take about as long | A (same answer). Timing not measured |
| 3 | Spoofing | API identity | Forged token, token with `alg: none`, expired token | HS256 signature checked with the secret from .env; algorithm fixed in the code; `exp` required; 15 min lifetime | C |
| 4 | Spoofing | Metrics, audit log | Client sends `X-Probe: 1` to hide its traffic from the alerts, or a fake `X-Real-IP` to fake the audit IP | Gateway removes `X-Probe` and overwrites `X-Real-IP` on every request | Planned in E4 (integration test) |
| 5 | Tampering | Payments | Changed amount: 0, negative, huge, 3 decimals, text; extra fields such as `"status": "completed"` | Strict input models: amount 1 to 1,000,000 with 2 decimals, ids in the seeded range, unknown fields refused (422); DB value errors mapped to 422 | E |
| 6 | Tampering | Other students' data | Student pays or registers for another student | Writes allowed only for the student's own id (not even admin) | D, F (payments and grades checksum unchanged) |
| 7 | Tampering | Schema, audit log | SQL injection or a compromised service tries DROP, ALTER, TRUNCATE, DELETE, or edits the audit log | Parameterized queries everywhere; services use `uni_app`: no DROP/ALTER (not the owner), no TRUNCATE or DELETE granted, audit_log INSERT only | J; SQL-like input refused in E |
| 8 | Repudiation | Payments, logins | A user says "I did not pay" or "that was not my login" | audit_log row (time, request_id, user, action, resource, result, source IP) plus a JSON log line; the request_id links gateway and service logs | I |
| 9 | Information disclosure | Student records | Student changes the id in the URL to read another student (IDOR) | Ownership check on every student endpoint; payments by id check the payment's owner | D |
| 10 | Information disclosure | Payments | Student resends another student's Idempotency-Key to see that payment | Key belonging to another student gets 409, without details | D |
| 11 | Information disclosure | Secrets | Passwords or the token secret leak through code, git, logs or images | All secrets in .env (git-ignored, `.env.example` committed); no Authorization header or password is logged; image built without .env | K |
| 12 | Information disclosure | Internal endpoints | Attacker calls `/admin/*`, mock-bank `/admin/fault` or `/metrics` | Not routed through the gateway (404); mock-bank, Prometheus and Grafana bound to 127.0.0.1 | D |
| 13 | Denial of service | Login, payments | Flood of requests from one client | nginx `limit_req`: login 5/s (burst 10), payments 20/s (burst 40), answered with 429; RateLimitSpike alert | H |
| 14 | Denial of service | Services | Very large or malformed bodies | Body limit 16 KB at the gateway (413); malformed JSON 422 | E |
| 15 | Denial of service | Student account | Attacker locks a victim out by failing 5 logins on purpose | Accepted risk: the lock lasts only 60 s, and the login rate limit slows the attacker down | G (lock length) |
| 16 | Elevation of privilege | Admin functions | Student calls batch transcripts, batch status or reconciliation | Role check (admin) on every admin endpoint | D |
| 17 | Elevation of privilege | Admin functions | Student forges a token with `role: admin` | Signature check: a token without the secret is refused | C |

## Known limitations (say these before you are asked)
- **Login needs the primary database (fail closed).** A login writes the failed-login counter (or resets it),
  and the replica is read-only, so while the primary is down every login answers 503. The other choice,
  allowing logins without counting failures, would let an attacker guess passwords without lockout during
  a DB outage. Tokens already issued stay valid for up to 15 min, so logged-in users are not affected.
- **No TLS.** Passwords and tokens travel as plain HTTP on localhost and inside the Docker network. A real
  deployment needs HTTPS at the gateway.
- **Shared HS256 secret.** Every service can verify tokens, so every service could also create them. If one
  service is compromised, tokens can be forged. Fix: RS256 with the private key only in the login service.
- **Rate limits per IP.** Many users behind one NAT share a limit, and an attack from many IPs is not
  slowed down. All our load generators run from one IP, so heavy load experiments will see 429.
- **Secrets as environment variables.** Anyone who can run `docker inspect` sees them. Docker secrets or a
  vault would be the next step.
- **One shared password for the 50 synthetic students** (synthetic data only).
- **Audit log in the same database.** The app cannot change or delete rows, but the DB owner can.
  A copy to separate storage would protect it from an admin as well.
