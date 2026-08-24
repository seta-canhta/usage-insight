# Cài đặt — 20 phút, làm một lần

Dành cho engineer tham gia pilot đo hiệu quả AI. macOS và Ubuntu.

**Nó ghi lại gì:** agent nào của platform đã chạy, trên ticket nào, tốn bao nhiêu token
Copilot. Tất cả nằm trên máy bạn cho tới khi chính bạn gửi file đi.

**Nó không bao giờ ghi:** prompt của bạn, câu trả lời của Copilot, code, diff,
nội dung file, hay bất cứ thứ gì bạn gõ. Chỉ có số đếm, hash và các nhãn cố định.
Bạn đọc được mọi file trước khi gửi, và xoá sạch bằng một lệnh.

---

## 1 · Một lệnh duy nhất

```bash
git clone git@github.com:seta-canhta/usage-insight.git
cd usage-insight
./insight setup --repo ~/work/repo-one --repo ~/work/repo-two
```

Lặp `--repo` cho từng repo bạn làm việc. Mỗi cái được ghi nhớ, nên `scan` về sau không cần tham số — và repo bạn quên chính là repo sẽ âm thầm không báo gì.

Lệnh này cấu hình VS Code, ghi nhận sự đồng ý của bạn, và cài commit hook.
Thêm `--dry-run` nếu muốn xem trước nó sẽ đổi gì.

Nó backup `settings.json`, **giữ nguyên mọi setting bạn đang có**, và **khôi
phục backup nếu kết quả không parse được** — nên file của bạn hoặc đúng, hoặc
không bị đụng tới. Làm tay việc này đã sai 3 lần trong một buổi chiều ở đây, và
**không lần nào báo lỗi cả**.

Sau đó **thoát hẳn VS Code rồi mở lại** (không phải Reload Window).

## 2 · ⚠️ Biết rõ `captureContent` KHÔNG làm được gì

**Nó không ngăn prompt của bạn lọt vào file.** Đây là lỗi đã biết và **chưa
sửa** của VS Code — [microsoft/vscode#326254](https://github.com/microsoft/vscode/issues/326254):
*"đường log và metric tôn trọng setting này; đường span thì không."* Vẫn tái
hiện trên copilot-chat 0.62.0.

Nghĩa là `~/.seta-insight/copilot-spans.jsonl` **có thể chứa prompt của bạn, câu
trả lời của Copilot, và output của lệnh nó chạy.** Hãy coi nó như ghi chú riêng.

Hai lớp bảo vệ phần dữ liệu **rời khỏi máy bạn**:

- `maxAttributeSizeChars: 256` cắt mọi attribute dài — làm rỗng các trường chứa
  nội dung, giữ nguyên id và số token.
- Bộ thu thập chỉ đọc **22 trường được đặt tên**. Mọi thứ khác bị loại trước khi
  lưu, có test khẳng định, và `./insight otel` xoá rỗng file thô sau khi đọc.

**Hãy tự mở file đó ra đọc** trước lần `pack` đầu tiên. Nó là JSON mỗi dòng. Thấy
gì không nên có thì báo trước khi gửi bất cứ thứ gì.

## 3 · Nối commit với agent đã tạo ra nó

Trong mỗi repo bạn làm việc:

```bash
cd ~/path/to/qa-automation
~/usage-insight/insight install-hook --repo .
```

Nó thêm một dòng `AI-Run-Id: run_abc123` vào **cuối** commit message khi có agent
đang chạy. Nó không đụng vào dòng tiêu đề commit, và **không bao giờ làm hỏng
commit** — có lỗi gì thì nó im lặng bỏ qua.

Nếu bạn đã có sẵn hook `prepare-commit-msg`, nó sẽ từ chối ghi đè. Gặp trường
hợp đó thì báo lại, đừng ép.

---

## Thứ Sáu hàng tuần — khoảng một phút

```bash
cd ~/usage-insight
./insight otel                                   # token của Copilot
./insight collect                                # agent nào, ticket nào
./insight scan      # mọi repo đã đăng ký
./insight pack --since 2026-08-17 --until 2026-08-23
```

`otel` xoá rỗng file span của Copilot sau khi đọc — không thì nó phình vô hạn,
và đó chính là file có thể chứa prompt của bạn.

`pack` in ra đường dẫn một file trong `~/.seta-insight/.reports/`. Gửi file đó.

**Cứ mở ra xem trước nếu muốn.** Nó là text thuần: một dòng tóm tắt, rồi mỗi
dòng một sự kiện. Không nén, không giấu gì.

**Tuần không làm gì vẫn phải gửi file.** Tuần không có sự kiện là số 0 thật; tuần
không có file là *thiếu dữ liệu*. Báo cáo phải phân biệt được hai thứ đó — nên
gửi một bundle rỗng cũng là một câu trả lời có ích.

---

## Quyền của bạn

| | |
|---|---|
| `./insight status` | đang có gì trong bộ đệm |
| `./insight purge --yes` | xoá sạch mọi sự kiện và bundle |
| `./insight purge --yes --all` | xoá luôn, và quên hẳn máy này |
| Tắt hoàn toàn | đặt `otel.enabled` về `false` và ngừng chạy `pack` |

## Đây không phải cái gì

Các con số này mô tả **một cách làm việc đang diễn ra thế nào** — việc có AI hỗ
trợ có được duyệt ngay lần đầu không, test có thực sự chạy không, một phiên tốn
bao nhiêu.

Nó **không phải hồ sơ đánh giá cá nhân**, và không đủ cơ sở để đánh giá ai cả.
Việc thu thập là tự nguyện, bạn đọc được mọi thứ trước khi nó rời máy bạn, và
xoá được bất cứ lúc nào. Điều đó là cố ý — và cũng chính là lý do dữ liệu này
không bao giờ có thể dùng làm bằng chứng kiểm tra ai đó.

Có thắc mắc, hoặc thấy gì đó lạ trong bundle: hỏi trước khi gửi.
