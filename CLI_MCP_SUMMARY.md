# CLI MCP Support - Summary

## ✅ Completado

Se ha agregado soporte completo para Model Context Protocol (MCP) en el CLI de Phoson.

## 🎯 Uso Rápido

```bash
# 1. Crear configuración
cat > phoson-mcp.json << 'EOF'
{
  "mcpServers": {
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
    }
  }
}
EOF

# 2. Iniciar CLI
phoson-cli

# 3. Habilitar MCP
> /mcp enable

# 4. Usar
> List files in /tmp
```

## 📊 Commits (5 nuevos)

1. **feat(cli): add MCP configuration support**
   - Añadido `enable_mcp` y `mcp_config_file` a PhosonConfig
   - Soporte para variables de entorno
   - Persistencia en config.toml

2. **feat(cli): integrate MCP plugin into REPL**
   - Carga automática del plugin MCP cuando está habilitado
   - Integración transparente con el agente
   - Recarga al cambiar modelo

3. **feat(cli): add /mcp command for runtime MCP management**
   - `/mcp status` - Ver estado y herramientas
   - `/mcp enable` - Habilitar MCP
   - `/mcp disable` - Deshabilitar MCP
   - `/mcp config <path>` - Cambiar archivo de configuración
   - `/mcp help` - Ayuda

4. **docs(cli): add comprehensive MCP CLI documentation**
   - Guía completa en docs/mcp-cli.md
   - Ejemplos de todos los servidores MCP oficiales
   - Troubleshooting y tips

5. **docs: add summary files for plugin system and MCP implementation**
   - Resúmenes y referencias rápidas

## 🔧 Comandos

| Comando | Descripción |
|---------|-------------|
| `/mcp status` | Mostrar estado y herramientas cargadas |
| `/mcp enable` | Habilitar MCP (persiste) |
| `/mcp disable` | Deshabilitar MCP (persiste) |
| `/mcp config <path>` | Cambiar archivo de configuración |
| `/mcp help` | Mostrar ayuda |

## ⚙️ Configuración

### Variables de Entorno

```bash
export PHOSON_ENABLE_MCP=true
export PHOSON_MCP_CONFIG=./my-mcp.json
phoson-cli
```

### Archivo de Configuración

`~/.phoson/config.toml`:
```toml
[defaults]
enable_mcp = true
mcp_config_file = "phoson-mcp.json"
```

### Runtime

```
> /mcp enable
> /mcp config ./custom-mcp.json
```

## 📦 Archivos

### Modificados
- `phoson_cli/config.py` - Configuración MCP
- `phoson_cli/repl.py` - Integración del plugin
- `phoson_cli/commands.py` - Comando /mcp

### Creados
- `docs/mcp-cli.md` - Documentación completa (443 líneas)

## ✨ Características

- ✅ Habilitación/deshabilitación en runtime
- ✅ Visualización de herramientas MCP cargadas
- ✅ Cambio dinámico de archivo de configuración
- ✅ Configuración persistente
- ✅ Soporte para variables de entorno
- ✅ Integración transparente con el agente

## 🚀 Ejemplo Completo

```bash
# Crear configuración con filesystem y memory
cat > phoson-mcp.json << 'EOF'
{
  "mcpServers": {
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "."]
    },
    "memory": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-memory"]
    }
  }
}
EOF

# Iniciar CLI
phoson-cli

# Habilitar MCP
> /mcp enable
MCP enabled  ·  saved

# Ver estado
> /mcp status
MCP: enabled
Config file: phoson-mcp.json
Loaded 2 MCP tool(s):
  • mcp_filesystem_call
  • mcp_memory_call

# Usar filesystem
> List all Python files in the current directory
[El agente usa mcp_filesystem_call automáticamente]

# Usar memory
> Remember that my favorite programming language is Python
[El agente usa mcp_memory_call]

> What's my favorite programming language?
[El agente recupera de la memoria]
```

## 🧪 Testing

```bash
# Verificar configuración
python -c "
from phoson_cli.config import PhosonConfig
config = PhosonConfig(enable_mcp=True)
print(f'MCP enabled: {config.enable_mcp}')
"

# Iniciar con MCP habilitado
export PHOSON_ENABLE_MCP=true
phoson-cli
```

## 📈 Estadísticas

- **Commits**: 5 nuevos (13 total en la rama)
- **Archivos modificados**: 3
- **Archivos creados**: 1 (documentación)
- **Líneas de código**: ~500
- **Líneas de documentación**: ~450

## 📚 Documentación

- **Guía completa**: [docs/mcp-cli.md](../docs/mcp-cli.md)
- **Plugin MCP**: [phoson_plugin_mcp/README.md](../phoson_plugin_mcp/README.md)
- **Sistema de plugins**: [PLUGIN_SYSTEM.md](../PLUGIN_SYSTEM.md)

## 🎓 Próximos Pasos

1. **Instalar dependencias**:
   ```bash
   pip install mcp
   npm install -g @modelcontextprotocol/server-filesystem
   npm install -g @modelcontextprotocol/server-memory
   ```

2. **Configurar servidores**:
   ```bash
   cp phoson-mcp.json.example phoson-mcp.json
   # Editar con tus configuraciones
   ```

3. **Usar en el CLI**:
   ```bash
   phoson-cli
   > /mcp enable
   > List files in /tmp
   ```

4. **Explorar servidores adicionales**:
   - GitHub (requiere GITHUB_PERSONAL_ACCESS_TOKEN)
   - Brave Search (requiere BRAVE_API_KEY)
   - PostgreSQL (requiere connection string)
   - Puppeteer (automatización de navegador)
   - Slack (requiere SLACK_BOT_TOKEN)

## 💡 Tips

1. **Empezar simple**: Usa `filesystem` o `memory` primero
2. **Variables de entorno**: No hardcodees API keys
3. **Múltiples configs**: Crea diferentes archivos para diferentes contextos
4. **Deshabilitar temporalmente**: Usa `/mcp disable` sin perder la configuración

## 🔗 Recursos

- [MCP Documentation](https://modelcontextprotocol.io/)
- [MCP Servers](https://github.com/modelcontextprotocol/servers)
- [Phoson Plugin System](../PLUGIN_SYSTEM.md)

---

**Estado**: ✅ Completamente funcional y listo para usar

**Versión**: 1.0.0
