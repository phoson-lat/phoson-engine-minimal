# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
and uses [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/).

## v0.5.0 (2026-08-19)

### Feat

- **cli**: one-shot mode, /undo command, sub-agent concurrency and timeout
- **cli**: /update command and shared self-update flow

### Fix

- **cli**: remove dead /branch command, close old engine on rebuild, harden config

## v0.4.0 (2026-08-19)

### Fix

- **examples**: update MCP plugin path after _plugin.py rename
- resolve provider SDK breakages, type-safety, logging, and refactors

## v0.3.0 (2026-08-09)

### Feat

- **memory**: close remaining phoson_plugin_memory gaps (prefix, CRUD tools, auto-purge)
- **memory**: add Qdrant semantic tier to phoson_plugin_memory
- **memory**: add Postgres long-term tier to phoson_plugin_memory
- **plugins**: add checkpoint/memory plugins, fix MCP session pooling

### Fix

- **cli**: preserve existing config on startup

## v0.2.4 (2026-05-15)

### Fix

- **gemini**: avoid leaking api key in model listing

## v0.2.3 (2026-05-15)

### Feat

- **cli**: stream markdown with rich live

## [v0.2.2] (2025-07-17)

### ✨ Features

#### phoson_cli — Full AI Provider Support

- (`provider_picker.py`) Expand provider picker from 4 to all 19 providers with
  labels for: GitHub Models, NVIDIA, Grok (X.AI), Groq, DeepSeek, Together AI,
  Perplexity, LM Studio, vLLM, Azure OpenAI, Google Gemini, Mistral AI,
  AWS Bedrock, Fireworks AI, Cohere
- (`model_selector.py`) Add model listing functions for all 15 new providers with
  automatic API discovery and graceful fallback on errors
- (`installer.py`) Expand setup wizard (`/setup`) to support all 19 providers:
  - Provider selection now shows all 19 providers to toggle
  - Credential prompts for each provider's API key / base URL
  - Summary table displays all configured providers and credentials
  - `_infer_enabled_providers` detects credentials for all 19 providers

### 📦 Version

- Bump version to 0.2.2

## [0.1.0] (2025-05-02)

### ⚡ Highlights

- First stable release of `phoson-engine-minimal`
- Framework-free Python runtime for the Phoson autonomous-agent platform
- Multi-provider LLM support: OpenAI, Anthropic, OpenRouter, Ollama

### ✨ Features

#### phoson_llm — LLM Normalization Layer
- (`21767dd`) Add utils module and public API exports
- (`2226570`) Add OllamaChat adapter for local LLM inference
- (`572da54`) Implement OpenRouterChat with new tool handling
- (`a238d3e`) Add multimodal input blocks for images, audio, video, documents
- (`9f22a2b`) Add subagent support with label field and AgentSubagentResult model
- (`d2130ec`) Increase max_tokens limit in ModelConfig to 32,768
- (`ac078f1`) Implement attachment manager for multimodal files in CLI
- (`597d7fb`) Add patch_file tool and line range params to read_file

#### phoson_agent — Agent Orchestration
- (`91a4261`) Add summarization and context window middleware plugins
- (`dfee0f8`) Add session metadata tracking and persistence in JSONL
- (`9f22a2b`) Add subagent support with label field and AgentSubagentResult model

#### phoson_cli — Interactive REPL
- (`605646a`) Add session delete command, load session, and token indicator
- (`c2e6c7d`) Add interactive session picker with pagination
- (`8ea0c33`) Add subagent tools (agent, agents) with build_tools_dict helper
- (`cc21ab5`) Add live subagent panel rendering and session metrics commands
- (`d4e4c93`) Add parallel sub-agent execution with metrics and UI

### 🐛 Bug Fixes

- (`740ad4b`) Update get_weather handler to accept context parameter
- (`a794177`) Improve formatting of metrics output in agents function
- (`afc079f`) Fix formatting issues

### 📚 Documentation

- (`7ef04f0`) Translate all docstrings to English
- (`e4ec1ab`) Improve docstring formatting for AgentEngine class
- (`c58b20e`) Add comprehensive docstrings to phoson_agent modules
- (`03897bf`) Add comprehensive docstrings to phoson_llm modules
- (`c39aae1`) Add comprehensive docstrings to all CLI modules
- (`bd23d10`) Expand README with full documentation and add CONTRIBUTING

### 🎨 Style

- (`c228160`) Apply ruff formatting to all modules
- (`4490006`) Fix import sorting with ruff

### 🔧 Refactor

- (`c39aae1`) Unify SessionMetrics and add BaseTool interface
- (`4fe6fe8`) Improve JSON schema generation for complex types
- (`168aac2`) Improve code readability and organization in various modules

### ✅ Tests

- (`1e0e177`) Improve formatting and readability in integration tests
- (`c067900`) Add edge case coverage (tool errors, empty response, max iterations, LLM protocol errors)
- (`3bf244b`) Add provider adapter integration coverage (OpenAI, OpenRouter, Anthropic, Ollama)
- (`7a2bd5f`) Add OpenAI adapter integration test
- (`060c1d0`) Add tests for subagent tools and renderer functionality
- (`c5b4e0f`) Add multimodal input tests for images, audio, video, and documents

### 📦 Dependencies

- Initial dependency set includes: anthropic, httpx, openai, prompt-toolkit, rich, tiktoken