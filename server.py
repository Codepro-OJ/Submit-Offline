from offline_oj.config import load_env

load_env()

from offline_oj.server import run  # noqa: E402


if __name__ == '__main__':
    run()
