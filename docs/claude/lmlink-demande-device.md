# Demande de fonctionnalité LM Studio : l'appareil d'une instance dans `/api/v1/models`

> Brouillon à publier par Ilyann (issue #77). Rédigé en anglais pour le
> dépôt de suivi de LM Studio. Mesuré le 2026-10-29 avec LM Studio 0.4.25+1
> et le SDK Python `lmstudio` 1.5.0. Tant que ce champ n'existe pas, Épure
> **ne peut pas savoir** où tourne le modèle quand LM Link est actif : on ne
> devine pas la machine (#77).

---

**Title:** Expose the device (name and identifier) of each loaded instance in `GET /api/v1/models`

**Use case.** With LM Link enabled, `lms load <model> -y` may place the model on a
linked device instead of the local machine ("Model loaded successfully on
<device>"), while the server keeps answering on the same `http://localhost:1234`.
A REST client has no way to tell where the instance runs. This matters to us:
settings we measured locally (e.g. `reasoning_effort: "none"`, which turns
reasoning off locally) were ignored once on the linked device, throughput differs
by machine, and the instance was unloaded mid-request on the linked device. We
would like to flag "model running on <device>" to the user and refuse to run
local benchmarks on a remote instance. Only `lms ps` (column `DEVICE`) shows it today.

**What we found (0.4.25+1).**
- `GET /api/v1/models` → `loaded_instances[]` carries `id`, `config`,
  `remaining_ttl_seconds`; no device field.
- `GET /api/v0/models/{id}`, the body and headers of `POST /v1/chat/completions`:
  no device either (`system_fingerprint` is the instance identifier).
- The SDK WebSocket `listLoaded` raw response does carry `deviceIdentifier`
  (`null` for a local instance), but no device name, and the Python SDK's typed
  `LlmInstanceInfo` does not declare it.

**Request.** Add a `device` object to each `loaded_instances[]` item, e.g.:

```json
"loaded_instances": [{
  "id": "huihui-qwen3.5-9b-abliterated",
  "config": { "context_length": 8192 },
  "device": { "identifier": "…", "name": "ilyann2", "is_local": false }
}]
```

`is_local` (or `identifier: null` for the local machine, as in the SDK) lets a
client decide without knowing its own device id.

**Reproduction.**
1. Machine A with LM Studio and LM Link enabled; machine B linked and connected.
2. On A: `lms load <model> -y`. The CLI may answer "Model loaded successfully on B";
   `lms ps` shows `DEVICE = B`.
3. On A: `curl http://localhost:1234/api/v1/models`. The entry's
   `loaded_instances[0]` has no field telling it runs on B (and the same model
   key may appear twice, once per device).
4. `POST /v1/chat/completions` on A succeeds and is served by B, with nothing in
   the response saying so.
