# Submit-Offline

Hệ thống OJ độc lập cho kỳ thi qua LAN. Thư mục này có web thí sinh, API, cơ sở dữ liệu riêng, dữ liệu đề và test riêng, worker chấm bài và container sandbox. Không gọi API, database hoặc Judge của `Codepro-Web`. Trong giờ thi không cần Internet. Bản chạy thử dùng SQLite; triển khai cho hàng trăm máy ở nhiều phòng dùng PostgreSQL, Redis và nhiều worker trên một máy chủ trung tâm.

Repository: [Codepro-OJ/Submit-Offline](https://github.com/Codepro-OJ/Submit-Offline). Dữ liệu cuộc thi thật, mật khẩu và bản sao lưu chỉ lưu trên máy chủ, không đưa lên Git.

## Cấu trúc

```text
Submit-Offline/
├── public/                 Web thí sinh và dashboard quản trị
├── offline_oj/             API, xác thực, database, hàng đợi, Judge
├── judge/Dockerfile        Image biên dịch/chạy mã thí sinh
├── config/                 Mẫu cấu hình contest
├── examples/               Bài mẫu để chạy thử
├── problems/               Đề và test thật (riêng tư, không đưa vào Git)
├── data/                   SQLite thử nghiệm và file tạm chấm bài (riêng tư)
├── manage.py               Nhập contest, tài khoản, xuất điểm, backup
├── server.py               Web + API cổng 8890
└── worker.py               Worker chấm bài độc lập
```

## Chuẩn bị trước khi ngắt Internet

Máy chủ cần Python 3.11+ và Docker Engine chạy Linux containers. Build image chấm khi còn Internet:

```powershell
docker build -t codepro-offline-sandbox:1 -f judge/Dockerfile .
```

Sao chép `.env.example` thành `.env`. Đặt `OFFLINE_CONTEST_KEY` đúng mã kỳ thi. Sửa `config/contest.example.json` hoặc tạo tệp JSON khác với giờ bắt đầu/kết thúc có UTC offset. Mỗi bài cần `problems/CODE/statement.md`, `tests.json` và các cặp `1.in`/`1.out`, `2.in`/`2.out`... Mỗi phần tử `tests.json` có điểm riêng (`{"points": 20}`). Có thể chấm theo nhóm bằng cách gắn cùng `group` và `group_points` cho mọi test trong nhóm; nhóm chỉ được điểm khi tất cả test đạt. Cấu hình bài hỗ trợ `checker: "token"` hoặc `"exact"`. Test và đáp án nằm ngoài `public/` và được `.gitignore` loại khỏi Git.

### Chạy thử cục bộ

Chạy thử với bài mẫu và SQLite:

```powershell
New-Item -ItemType Directory -Path problems -Force
Copy-Item examples/SUM problems/SUM -Recurse
python manage.py import-contest config/contest.example.json
python manage.py add-user hs001
python manage.py add-admin operator
python manage.py enroll hs001 DEMO2026
python manage.py preflight DEMO2026
```

Mở hai terminal trong `Submit-Offline`:

```powershell
python server.py
python worker.py
```

Thí sinh truy cập `http://<IP-máy-chủ>:8890`, ví dụ `http://192.168.10.10:8890`. Ban tổ chức dùng `http://<IP-máy-chủ>:8890/admin` với tài khoản tạo bằng `add-admin` để xem hàng chờ, bài nộp và nhật ký. Chỉ mở cổng 8890 cho mạng thi; không cấp quyền truy cập Docker socket, file `data/` hoặc `problems/` cho máy thí sinh. Dùng switch/VLAN để ngăn các máy thi kết nối trực tiếp với nhau. Đồng bộ đồng hồ máy chủ trước kỳ thi vì giờ mở/đóng dùng đồng hồ server.

### Triển khai nhiều phòng thi

Đặt mã nguồn ở một đường dẫn tuyệt đối trên máy chủ Linux, ví dụ `/srv/submit-offline`. Trong `.env`, đặt `OFFLINE_PROJECT_DIR` bằng đường dẫn đó, đặt `OFFLINE_DB_PASSWORD` là chuỗi ngẫu nhiên riêng và `OFFLINE_CONTEST_KEY` là mã cuộc thi. Tạo các thư mục `problems/`, `data/tmp/`, `backups/`, `exports/`; chép bộ đề và test riêng tư vào `problems/`. Build toàn bộ image trước khi ngắt Internet:

```sh
docker build -t codepro-offline-sandbox:1 -f judge/Dockerfile .
docker compose -f compose.yml build
docker compose -f compose.yml up -d postgres redis init
docker compose -f compose.yml run --rm web python manage.py import-contest config/contest.example.json
docker compose -f compose.yml run --rm -v /secure/contestants.csv:/tmp/contestants.csv:ro web python manage.py import-users /tmp/contestants.csv DEMO2026
docker compose -f compose.yml run --rm web python manage.py add-admin operator
docker compose -f compose.yml run --rm worker python manage.py preflight DEMO2026
docker compose -f compose.yml up -d --scale worker=8 web worker
```

`contestants.csv` có cột `username,password`, mỗi dòng là một thí sinh; giữ file này ngoài web và xóa bản không cần thiết sau khi nhập. `OFFLINE_DB_PASSWORD` nên dùng ký tự chữ và số để URL kết nối không cần mã hóa thêm. Giá trị `8` worker chỉ là điểm khởi đầu, cần chạy thử tải trên phần cứng thật để chọn số phù hợp. PostgreSQL và Redis không mở cổng ra LAN. Bài nộp được ghi bền vững vào PostgreSQL; Redis chỉ đánh thức worker. Các worker nhận bài khác nhau bằng khóa hàng của PostgreSQL. Toàn bộ máy thi ở các phòng kết nối tới cùng IP máy chủ qua mạng LAN/VLAN.

## Vận hành

- Contest và tài khoản được chuẩn bị bằng `manage.py` trước giờ thi. Người dùng chưa được `enroll` không xem đề hoặc nộp bài được.
- Bài nộp được ghi vào database trước khi trả mã submission. Worker lấy bài theo thứ tự, chấm C++17, C++20 hoặc Python 3 và tính điểm theo `points` hoặc nhóm `group` trong `tests.json`.
- Mỗi lượt biên dịch/chạy mở một container Docker không có mạng, giới hạn CPU/RAM/PID, hạ quyền và chỉ gắn file cần thiết. Đáp án `.out` không được gắn vào container chạy bài thí sinh.
- Giám sát bằng `python manage.py status DEMO2026`. Bài lỗi hệ thống `IE` có thể đưa vào hàng đợi lại bằng `python manage.py requeue ID` sau khi khắc phục nguyên nhân.
- Sau thi, chạy `python manage.py export-results DEMO2026 exports/results.csv` và `python manage.py backup backups/exam.zip` (hoặc chạy các lệnh này qua `docker compose run --rm web` khi dùng Compose). Bản backup chứa database, mã nguồn bài nộp và test, nên cần cất ở nơi chỉ ban tổ chức truy cập. Với PostgreSQL, backup chứa `data/database.sql`.
- Chạy `python -m unittest discover -s tests` để kiểm tra API, `python tests/judge_smoke.py` để thử chấm bằng Docker, và `python tests/postgres_smoke.py` để thử PostgreSQL cùng nhiều worker nhận bài song song. Bài thử PostgreSQL cần `pip install -r requirements.txt` và `docker build --target web -t codepro-offline-web:test .`.

GitHub Actions chạy unit test và kiểm tra cú pháp trên mỗi lần đẩy mã. Hai bài smoke dùng Docker cần chạy trước kỳ thi trên máy có Docker Engine.

Mẫu `DEMO2026` dùng khoảng thời gian rất dài chỉ để thử phần mềm. Trước kỳ thi thật, thay bằng thời gian chính xác, bộ test chính thức và mật khẩu riêng cho từng thí sinh. Hệ thống hiện xuất CSV và bản backup; chưa có đồng bộ tự động kết quả vào `Codepro-Web`.
