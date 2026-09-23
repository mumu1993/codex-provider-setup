# Copy this request to your AI assistant

Help me configure Codex with https://github.com/mumu1993/codex-provider-setup.

1. Read the repository README and the script before running it. Check macOS, Python 3.11+, Codex login and whether CC Switch is installed. Do not log me out or change my personal auth.json.
2. Ask for my gateway's non-sensitive documentation or access page if it is missing. Use my exact authorized model IDs, supported protocol, base URL and reasoning levels. Never infer access from a similar model name. Never put credentials into the repository or chat.
3. Create a private JSON manifest outside the checkout using config.example.json as the schema. The example endpoints and model capabilities are placeholders, not a real account configuration.
4. If I use CC Switch, make sure its Codex local routing is disabled and the app is fully closed. The installer should create only its own two provider cards and preserve all other providers.
5. Run the dry-run command first. Explain what it will change. Get the API Key through the tool's hidden terminal input, an existing environment variable or a mode-600 local key file; do not print it or put it in shell arguments.
6. Run setup with --with-cc-switch only when appropriate. Keep the backup path. Verify the model catalog and short streaming responses; --verify-all makes a small paid request to each model.
7. Tell me how to select Personal ChatGPT / Team Gateway in CC Switch and reopen Codex. Existing tasks may retain their previous connection. If a check fails, fix or roll back the configuration instead of claiming success.

My gateway documentation: [provide URL or non-sensitive details]
My authorized model list: [provide exact IDs or an access-page URL]
Use CC Switch: [yes/no]
