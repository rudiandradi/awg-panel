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

## Russian sites outside the VPN tunnel

The panel downloads the current `whitelist` and `private` IPv4 CIDR lists from RoscomVPN GeoIP
once a day and exposes them as an Amnezia import file. This is the compatible set: Amnezia
rejects the much larger full `direct` list (more than 42,000 CIDRs). This changes no AWG server
or peer configuration.

In the panel, download **"Скачать для Amnezia"**. On each client, open the site/IP split
tunneling settings, select **"Адреса из списка не должны использовать VPN"**, import the
downloaded JSON file, then reconnect the VPN. The rules are IPv4-only because that is what
Amnezia's IP split tunneling supports. Re-download and re-import the file after future panel
updates; Amnezia clients do not support a remote, self-updating rule list.

Set `ROSCOMVPN_REFRESH_SECONDS` to change the server-side refresh interval. The default is one
day. The source URLs can be overridden with `ROSCOMVPN_DIRECT_URL`, `ROSCOMVPN_WHITELIST_URL`,
and `ROSCOMVPN_PRIVATE_URL`. Set `ROSCOMVPN_INCLUDE_DIRECT=true` only if a specific client is
known to accept the full list.
