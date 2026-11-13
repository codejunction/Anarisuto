import os

# Keep test runs offline and quiet: no CrewAI tracing or telemetry.
for key, value in {"CREWAI_TRACING_ENABLED": "false", "CREWAI_DISABLE_TELEMETRY": "true",
                   "OTEL_SDK_DISABLED": "true"}.items():
    os.environ.setdefault(key, value)
