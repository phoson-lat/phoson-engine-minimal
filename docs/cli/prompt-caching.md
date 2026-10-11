# Prompt caching

Long conversations re-send the whole history on every turn; phoson-cli
keeps that prefix cacheable so providers can bill it at the (much
cheaper) cache-read rate instead of full input price. It is on by
default, no configuration needed:

- **Anthropic** — each request carries ephemeral `cache_control`
  breakpoints on the three stable parts of the prompt: the system prompt,
  the tool list, and the end of the conversation history (which advances
  as it grows). Cached usage shows up as `cache_creation` / `cache_read`
  tokens.
- **OpenRouter** — the conversation's session id is sent as `session_id`
  (sticky routing) so OpenRouter pins you to one upstream provider and
  its cache stays warm from the first turn; `anthropic/*` models
  additionally opt into automatic caching. The adapter also identifies
  itself as *phoson-cli* in OpenRouter's app rankings.
- **Alibaba Cloud Model Studio** — explicit caching is on for the Qwen models
  that document it (Qwen Max / Plus / Flash / Coder / VL, plus some
  DeepSeek/Kimi/GLM ids; models without support such as `qwen-turbo` stay on
  the implicit path): the stable prefix (system prompt **and** the tool list,
  which Model Studio counts as part of the system prompt) plus the end of the
  conversation carry `cache_control: {"type": "ephemeral"}` markers, so each
  turn hits the cache the previous turn created. Cache writes cost ~125% of the
  input price and hits ~10% — except the `qwen3.8-*` family, whose hit rate is
  lower (~8.5% of input) and is priced per model. A cache entry lives 5 minutes,
  renewed on every hit. A marker only takes effect once the cached block reaches
  1024 tokens, so short prompts stay on Model Studio's implicit-cache path.
  Cached usage shows up as `cache_creation_input_tokens` (write) /
  `cached_tokens` (read).

The system prompt is deliberately a **stable prefix** (date + timezone,
not a live clock) so it does not bust the cache between turns. Cached
tokens accumulate in the session metrics and surface in `/status`
(`cache  R read / W write`) and `/tokens` (`cache=Rr/Ww`). Cached reads
cost 10–50% of the base input price, so a warm cache typically cuts
long-session prompt cost by 50–90%. See `docs/api/phoson_llm.md` for
per-provider details.
