# Invoice MCP Server

An MCP server that exposes invoice tools (`list_invoices`, `get_invoice`,
`list_items`, `create_invoice`) by calling the Accounting Software app's HTTP API.

## Run locally (stdio — for Claude Desktop on your machine)
```
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python invoice_server.py      # talks to http://localhost:3000 by default
```

## Run hosted (HTTPS — for sharing across machines)
Set these environment variables:

| Variable | Meaning |
|----------|---------|
| `MCP_TRANSPORT=http` | Serve over HTTPS instead of stdio |
| `INVOICE_API_BASE` | Public URL of the deployed accounting app |
| `INVOICE_API_SECRET` | Must match the app's `API_SECRET` |
| `PORT` | Provided automatically by the host (Render) |

See `../DEPLOY.md` for the full step-by-step deployment guide.
```
MCP_TRANSPORT=http INVOICE_API_BASE=https://your-app.onrender.com \
  INVOICE_API_SECRET=yoursecret python invoice_server.py
```
