# RDS certificate bundle

`rds-global-bundle.pem` is Amazon's public bundle of RDS certificate
authorities. The image copies it to `/srv/certs/`, and the application uses it
to verify the database server's certificate and hostname whenever
`DATABASE_URL` has `sslmode=verify-full` (the default). It contains CA
certificates only; nothing in it is secret.

| | |
| --- | --- |
| Source | https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem |
| Downloaded | 2026-10-28 |
| SHA-256 | `90a6f1a7978a1b1b096dca6c57511ca48d3b8cf052f8e704baf358f34ea2f21b` |

To refresh it, download the same URL over this file, record the new checksum
and date here, and rebuild the image. AWS announces CA rotations in the RDS
documentation; a database using a CA that is not in the bundle fails
readiness with `database unavailable` and a `CertificateVerifyError`.
