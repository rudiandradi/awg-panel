# AWG Panel

Mobile-friendly web panel for the `amnezia-awg2` container.

## Run

```bash
cp .env.example .env
openssl rand -base64 24
```

Put the generated value into `PANEL_PASSWORD`, set `VPN_ENDPOINT` to your public AWG endpoint, and set `CERT_DIR` if your TLS certificate is not in `./cert`. Then:

```bash
docker compose up -d --build
```

Open:

```text
https://SERVER_IP:8443
```

## Notes

- Peer names and disabled state are stored in `./data/peers.json`.
- Traffic totals are stored in `./data/peers.json` and survive disable/enable cycles.
- New peers can be created from the UI. Their client config and QR source are stored in `./data/peers.json`.
- QR/config can be shown again only for peers created by this panel, because existing peers' client private keys are not available on the server.
- Disabling a peer removes it from the live `awg0` interface after saving its current runtime config.
- Enabling a peer restores it from `./data/peers.json`.
- If `amnezia-awg2` is recreated with different keys and the panel has not seen them, old disabled peers cannot be restored automatically.
- The panel mounts `/var/run/docker.sock`; expose it only behind a firewall/VPN/reverse proxy.
- The included compose file serves HTTPS using `fullchain.pem` and `privkey.pem` from `CERT_DIR` or `./cert`.
