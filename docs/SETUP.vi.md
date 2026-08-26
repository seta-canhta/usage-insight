# Cài đặt — 20 phút, làm một lần

Dành cho engineer tham gia pilot đo hiệu quả AI. macOS và Ubuntu.

**Nó ghi lại gì:** agent nào của platform đã chạy, trên ticket nào, tốn bao nhiêu token
Copilot. Tất cả nằm trên máy bạn cho tới khi chính bạn chạy một lệnh để gửi đi.

**Nó không bao giờ ghi:** prompt của bạn, câu trả lời của Copilot, code, diff,
nội dung file, hay bất cứ thứ gì bạn gõ. Chỉ có số đếm, hash và các nhãn cố định.
Bạn đọc được mọi file trước khi gửi, và xoá sạch bằng một lệnh.

---

## 0 · Máy đã thu thập sẵn? Hai lệnh

Nếu máy này đã chạy `insight` một thời gian và anh/chị vừa được thêm vào
whitelist của server thì không phải làm lại từ đầu — dữ liệu đã thu thập giữ
nguyên, máy giữ nguyên định danh và salt.

```bash
git pull
./insight setup --token <secret được gửi> --repo ~/work/<từng repo>
./insight ship --all        # gửi hết những gì đã thu thập
```

**`--repo` quan trọng.** Máy không đăng ký repo nào thì không thu được gì từ
git, nhưng vẫn upload mỗi ngày một bundle báo 0 sự kiện — trông y hệt một ngày
thật sự không làm gì, và phía sau không phân biệt được. `setup` sẽ nhắc nếu
quên, và `./insight status` liệt kê repo đã đăng ký.

Không cần kèm email: server tự tra secret ra người — đó chính là ý nghĩa của
whitelist — và `ship` chưa bao giờ gửi địa chỉ email trong bất kỳ header nào.

Sau khi upload thành công lần đầu:

```bash
./insight rotate-token      # rồi gửi lại dòng nó in ra
```

Bước cuối quan trọng. Secret được *gửi* tới thì đã đi qua một kênh nào đó; secret
tự tạo trên máy thì không. Sau khi rotate, máy này giữ một secret chưa ai từng
thấy, và secret cũ vẫn upload được cho tới khi server cập nhật — không cần canh
đúng thời điểm.

---

## 1 · Một lệnh duy nhất

```bash
git clone git@github.com:seta-canhta/usage-insight.git
cd usage-insight
./insight setup --repo ~/work/repo-one --repo ~/work/repo-two \
    --email ban@seta-international.vn
```

Lặp `--repo` cho từng repo bạn làm việc. Mỗi cái được ghi nhớ, nên `scan` về sau không cần tham số — và repo bạn quên chính là repo sẽ âm thầm không báo gì.

Lệnh này cấu hình VS Code, ghi nhận sự đồng ý của bạn, cài commit hook, và tạo
một khoá bí mật để upload. Thêm `--dry-run` nếu muốn xem trước nó sẽ đổi gì.

### Một dòng cần gửi đi

Kết thúc `setup`, nó in ra một dòng như thế này:

```
    ban@seta-international.vn:9f2ac41e8b...c3d1
```

**Gửi dòng đó cho người vận hành pipeline.** Nó được thêm vào danh sách cho phép
trên server, và `./insight ship` sẽ từ chối upload cho tới khi dòng đó có mặt.

Dòng đó là *hash*, không phải mật khẩu — dán vào chat cũng an toàn. Khoá bí mật
thật được tạo ngay trên máy bạn, nằm trong `~/.seta-insight/config.json`, và
không bao giờ được gửi cho ai, kể cả người quản lý danh sách cho phép.

Mất rồi? `./insight whoami` in lại.

Email của bạn chỉ dùng cho một việc: cho server biết ai đang upload, để tuần nào
thiếu dữ liệu thì hỏi đúng người. **Nó không bao giờ được ghi vào bundle** — dữ
liệu thu thập không chứa địa chỉ email nào cả.

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

## Nó tự chạy, mỗi giờ

`setup` đã hẹn lịch sẵn. Mỗi giờ máy bạn đọc dữ liệu, đóng gói sự kiện trong
ngày, và **chỉ upload nếu có thay đổi** — giờ nào không có gì thì không gửi gì.

| | |
|---|---|
| `./insight schedule --status` | đang bật không, chạy lần cuối lúc nào |
| `./insight schedule --off` | tắt; quay lại tự chạy lệnh bằng tay |
| `~/.seta-insight/auto.log` | mỗi lần chạy một dòng, kể cả lỗi |

Không chạy bằng quyền root, không cài gì ngoài thư mục home của bạn.

Mọi bundle đã gửi vẫn nằm trong `~/.seta-insight/.reports/` để bạn mở ra đọc lại
bất cứ lúc nào, và `purge` vẫn xoá sạch mọi thứ trên máy bạn.

---

## Nếu bạn muốn tự chạy bằng tay

`./insight setup --no-schedule` để tắt lịch tự động. Khi đó, thứ Sáu hàng tuần —
khoảng một phút

```bash
cd ~/usage-insight
./insight otel                                   # token của Copilot
./insight collect                                # agent nào, ticket nào
./insight scan      # mọi repo đã đăng ký
./insight pack --since 2026-08-17 --until 2026-08-23
./insight ship      # upload lên
```

`otel` xoá rỗng file span của Copilot sau khi đọc — không thì nó phình vô hạn,
và đó chính là file có thể chứa prompt của bạn.

`pack` ghi một file trong `~/.seta-insight/.reports/`, còn `ship` upload nó lên.
Cố ý tách làm hai lệnh: **không gì rời khỏi máy bạn cho tới khi bạn gõ lệnh thứ
hai.**

**Cứ mở ra xem trước nếu muốn.** Nó là text thuần: một dòng tóm tắt, rồi mỗi
dòng một sự kiện. Không nén, không giấu gì. `./insight ship --dry-run` cho xem
sẽ gửi gì mà không gửi thật.

Chạy `ship` hai lần cũng không sao — server nhận ra bundle nó đã có và báo lại.
Không chắc tuần trước đã gửi được chưa thì cứ chạy lại.

**Tuần không làm gì vẫn phải gửi bundle.** Tuần không có sự kiện là số 0 thật;
tuần không có bundle là *thiếu dữ liệu*. Báo cáo phải phân biệt được hai thứ đó
— vì vậy `pack --since ... --until ...` ghi lại đúng tuần bạn muốn nói tới, kể
cả khi tuần đó không có gì.

---

## Quyền của bạn

| | |
|---|---|
| `./insight status` | đang có gì trong bộ đệm, và đã upload những gì |
| `./insight ship --dry-run` | sẽ gửi gì, mà không gửi thật |
| `./insight whoami` | in lại dòng cho danh sách cho phép |
| `./insight rotate-token` | đổi khoá upload (upload vẫn chạy bình thường) |
| `./insight purge --yes` | xoá sạch mọi sự kiện và bundle trên máy này |
| `./insight purge --yes --all` | xoá luôn, và quên hẳn máy này |
| Tắt hoàn toàn | đặt `otel.enabled` về `false` và ngừng chạy `pack` |

`purge` chỉ xoá trên máy bạn. Bundle đã upload thì đã upload rồi — cần xoá thì
hỏi người vận hành pipeline.

### Nếu upload thất bại

`ship` sẽ nói rõ là trường hợp nào, và không trường hợp nào làm mất bundle —
nó vẫn nằm trong `~/.seta-insight/.reports/` và lần chạy sau sẽ gửi lại.

| thông báo | nghĩa là gì |
|---|---|
| *not on the whitelist* (`401`) | dòng của bạn chưa tới server, hoặc bạn vừa đổi khoá và khoá cũ đã hết hiệu lực. Chạy `./insight whoami` để in lại |
| *did not reach the endpoint* | một thứ đứng trước server — CDN hoặc proxy — đã chặn request. Máy bạn không cần sửa gì; báo cho người vận hành pipeline |
| *a certificate this machine cannot verify* | Python trên máy bạn không có chứng chỉ CA. Với bản cài từ python.org trên macOS, chạy `/Applications/Python 3.x/Install Certificates.command` |
| *unreachable after 3 attempts* | mạng, hoặc server đang tắt. Lần chạy theo giờ kế tiếp sẽ gửi lại |

## Đây không phải cái gì

Các con số này mô tả **một cách làm việc đang diễn ra thế nào** — việc có AI hỗ
trợ có được duyệt ngay lần đầu không, test có thực sự chạy không, một phiên tốn
bao nhiêu.

Nó **không phải hồ sơ đánh giá cá nhân**, và không đủ cơ sở để đánh giá ai cả.
Việc thu thập là tự nguyện, bạn đọc được mọi thứ trước khi nó rời máy bạn, và
xoá được bất cứ lúc nào. Điều đó là cố ý — và cũng chính là lý do dữ liệu này
không bao giờ có thể dùng làm bằng chứng kiểm tra ai đó.

Có thắc mắc, hoặc thấy gì đó lạ trong bundle: hỏi trước khi gửi.
