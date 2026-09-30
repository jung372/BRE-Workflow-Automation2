"""python -m news: a single service process with threaded HTTP and one DB writer."""
import os


def main():
    from waitress import serve
    from .service import create_app
    app = create_app()
    try:
        serve(app, host=os.environ.get("WIND_NEWS_HOST", "127.0.0.1"), port=int(os.environ.get("WIND_NEWS_PORT", "8090")), threads=4, expose_tracebacks=False)
    finally:
        app.extensions["wind_news_runner"].stop()
        app.extensions["wind_news_store"].close()


if __name__ == "__main__":
    main()
