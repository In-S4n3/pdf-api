import os


ENVIRONMENT = os.environ.get("ENVIRONMENT", "production")
API_KEY = os.environ.get("API_KEY", "")
DEBUG = ENVIRONMENT == "development"
