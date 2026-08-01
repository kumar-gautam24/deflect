# Deflect web

The user-facing surface: an ask page, an eval dashboard and a trace list.

It holds no model credentials. `/ask` is proxied through a route handler to the answer
service so provider keys stay server-side, and the eval and trace pages read from the
service that owns that data.

| variable | default | used for |
| --- | --- | --- |
| `ANSWER_URL` | `http://localhost:8002` | the ask proxy and the trace list |
| `EVALS_URL` | `http://localhost:8003` | the eval dashboard |

```bash
npm install
npm run dev     # expects the answer and evals services to be running
npm test        # component tests for the answer panel and the run diff
```

See the repository README for the architecture and the measured results.
