from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    PROJECT_NAME: str = "backend-dataris"
    API_V1_STR: str = "/api"

    JWT_SECRET_KEY: str
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24

    DATABASE_URL: str
    BACKEND_CORS_ORIGINS: str = "*"

    # Pydantic v2 way to configure settings
    model_config = {
        "env_file": ".env",
        "extra": "ignore",  # ignores unknown env vars like instance_name, password
    }


settings = Settings()
