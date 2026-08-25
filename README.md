# AWG Panel

Mobile-friendly web panel for the `amnezia-awg2` container.

## Run

Choose a public hostname that resolves to the server. A wildcard DNS service such as
`awg.203.0.113.10.nip.io` works without creating DNS records. Make sure TCP ports 80/443
and UDP port 443 are reachable, then:

```bash
cp .env.example .env
docker run --rm caddy:2-alpine caddy hash-password --plaintext 'YOUR_PASSWORD'
```

Set `PANEL_PASSWORD` to the same password used by Caddy, `PANEL_DOMAIN` to the public
hostname, and `PANEL_AUTH_HASH` to the generated bcrypt hash. Keep the hash single-quoted
in `.env` so Docker Compose does not expand its `$` characters. Also set `VPN_ENDPOINT`
and `CERT_DIR` for the internal panel endpoint. Then:

```bash
docker compose up -d --build
```

Open the public URL:

```text
https://awg.SERVER_IP.nip.io
```

Caddy obtains and renews the public TLS certificate automatically. The panel remains bound
to `https://127.0.0.1:8443` for local maintenance.

## Notes

- Peer names and disabled state are stored in `./data/peers.json`.
- Traffic totals are stored in `./data/peers.json` and survive disable/enable cycles.
- New peers can be created from the UI. Their client config and QR source are stored in `./data/peers.json`.
- Client exports support AWG 2.x and automatically include live AWG 3.x/3.1 device parameters when enabled on the server.
- Existing panel-created client exports are regenerated from the live interface when opened, so protocol and endpoint changes are reflected without recreating the peer.
- QR/config can be shown again only for peers created by this panel, because existing peers' client private keys are not available on the server.
- Disabling a peer removes it from the live `awg0` interface after saving its current runtime config.
- Enabling a peer restores it from `./data/peers.json`.
- If `amnezia-awg2` is recreated with different keys and the panel has not seen them, old disabled peers cannot be restored automatically.
- The panel mounts `/var/run/docker.sock`; expose it only behind a firewall/VPN/reverse proxy.
- Caddy protects the public endpoint with Basic Auth; the internal panel keeps its own authentication and TLS.

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
