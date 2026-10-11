# Start the chaos controller + dashboard with Gemini as the AI model.
# The key is read from the GEMINI_API_KEY user environment variable, never from this file:
#   [Environment]::SetEnvironmentVariable('GEMINI_API_KEY', '<your key>', 'User')
$key = [Environment]::GetEnvironmentVariable('GEMINI_API_KEY', 'User')
if (-not $key) { Write-Error "GEMINI_API_KEY is not set for this Windows user"; exit 1 }
$env:CHAOS_LLM_PROVIDER = 'openai'
$env:CHAOS_OPENAI_URL = 'https://generativelanguage.googleapis.com/v1beta/openai'
# gemini-3.5-flash: the most reliable on the free tier when tested (newer flash models often return 503)
if (-not $env:CHAOS_OPENAI_MODEL) { $env:CHAOS_OPENAI_MODEL = 'gemini-3.5-flash' }
$env:OPENAI_API_KEY = $key
# tried in order when the main model answers 429/503 (overloaded)
if (-not $env:CHAOS_OPENAI_FALLBACK_MODELS) { $env:CHAOS_OPENAI_FALLBACK_MODELS = 'gemini-3.1-flash-lite,gemini-flash-latest' }
# keep a slow model from holding up the self-healing loop; rules take over on timeout
if (-not $env:CHAOS_LLM_TIMEOUT_S) { $env:CHAOS_LLM_TIMEOUT_S = '40' }
& "$PSScriptRoot\.venv\Scripts\python.exe" -m chaos serve @args
