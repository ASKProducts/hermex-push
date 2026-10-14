"""Shared code for the hermex-push plugin.

Imported by both the plugin entry point (``plugin/__init__.py``) and the dashboard route
(``dashboard/plugin_api.py``). The dashboard loads its API file standalone, outside the plugin
package, so both callers put the plugin root on ``sys.path`` and import this package by name.
"""

PLUGIN_NAME = "hermex-push"
PLATFORM_NAME = "hermex"
RELAY_URL_ENV = "HERMEX_PUSH_RELAY_URL"

# Bump on every merged change under plugin/ (patch for fixes, minor for features), together with
# plugin.yaml, pyproject.toml and dashboard/manifest.json; a test keeps the four in step. The
# pairing route reports this loaded value, so Hermex can tell an outdated or not-yet-restarted host.
PLUGIN_VERSION = "0.6.0"

# The identifier Hermex sends to ``POST /api/dashboard/agent-plugins/install``. hermes-agent
# splits it at ``.git/`` into the clone URL and the ``plugin`` subdirectory.
INSTALL_IDENTIFIER = "https://github.com/uzairansaruzi/hermex-push.git/plugin"

__all__ = ["PLUGIN_NAME", "PLATFORM_NAME", "RELAY_URL_ENV", "PLUGIN_VERSION", "INSTALL_IDENTIFIER"]
