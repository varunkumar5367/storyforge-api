# Generates JWT_SECRET_KEY and prints instructions (does not auto-write to .env)
.venv\Scripts\python -c "import secrets; k=secrets.token_urlsafe(48); print(''); print('Add this line to your .env file:'); print(''); print(f'JWT_SECRET_KEY={k}'); print('')"
