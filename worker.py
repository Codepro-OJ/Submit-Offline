from offline_oj.config import load_env

load_env()

from offline_oj.judge import run_worker  # noqa: E402


if __name__ == '__main__':
    run_worker()
