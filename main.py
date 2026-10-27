import socket
from app import create_app
from config import Config, STORAGE_DIR, DATA_DIR

app = create_app()


def _local_ips():
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    return sorted(ip for ip in ips if not ip.startswith('127.'))


if __name__ == '__main__':
    from waitress import serve
    print('=' * 56)
    print(f'  {Config.DISK_NAME} запущен')
    with app.app_context():
        from app.storage import storage_root
        root = storage_root()
    print(f'  Хранилище:         {root or "выберите при первом входе"}')
    print(f'  База и корзина:    {DATA_DIR}')
    print(f'  На этом ПК:        http://localhost:{Config.PORT}')
    for ip in _local_ips():
        print(f'  В домашней сети:   http://{ip}:{Config.PORT}')
    print('  Остановить: Ctrl+C или закрыть это окно')
    print('=' * 56)
    serve(app, host=Config.HOST, port=Config.PORT, threads=8,
          max_request_body_size=Config.MAX_CONTENT_LENGTH)
