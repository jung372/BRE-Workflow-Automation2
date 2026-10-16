"""python -m news: a single service process with threaded HTTP and one DB writer."""
import os


def main():
    from waitress import serve
    from .service import create_app
    from .profiles import profile_for
    profile = profile_for(os.environ.get("BRE_NEWS_INDUSTRY", "wind"))
    app = create_app()
    try:
        serve(app, host=profile.env("HOST", "127.0.0.1"), port=int(profile.env("PORT", "8091" if profile.industry == "solar" else "8090")), threads=4, expose_tracebacks=False)
    finally:
        app.extensions["wind_news_runner"].stop()
        app.extensions["wind_news_store"].close()


if __name__ == "__main__":
    main()
