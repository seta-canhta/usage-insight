# Cài đặt — 10 phút, làm một lần

Dành cho engineer tham gia pilot đo hiệu quả AI. macOS và Ubuntu.

**Nó ghi lại gì:** agent nào của platform đã chạy, trên ticket nào, tốn bao nhiêu
token và bao nhiêu premium request của Copilot. Tất cả nằm trên máy bạn cho tới
lúc được upload — mỗi giờ một lần nếu bạn đồng ý, hoặc lúc bạn tự gõ `ship`.

**Nó không bao giờ ghi:** prompt của bạn, câu trả lời của Copilot, code, diff,
nội dung file, hay bất cứ thứ gì bạn gõ. Chỉ có số đếm, hash và các nhãn cố định.
Bạn đọc được mọi file trước khi gửi, và xoá sạch bằng một lệnh.

**Nó không nhìn thấy gì:** panel **Copilot Chat** trong VS Code và inline
completion. Nguồn dữ liệu là session journal của chính Copilot CLI, và hai bề mặt
đó không ghi gì vào đó cả. Mỗi lần đọc đều nói rõ điều này — xem §2.

---

## 0 · Máy đã thu thập sẵn? Chạy lại trình cài đặt

Nếu máy này đã chạy `insight` một thời gian — từ một bản clone, hay từ một bản
cài cũ hơn — thì không phải làm lại từ đầu.

```bash
curl -fsSL https://aeris-insight.seta-international.com/install | sh
insight status
```

**Không có gì đã thu thập bị đụng tới.** Trình cài đặt không chạm vào
`~/.seta-insight/`: config, machine id, salt và khoá upload của bạn đều giữ
nguyên, nên dữ liệu đã nằm trong bộ đệm vẫn ở đó và dòng của bạn trên whitelist
của server vẫn dùng được. Lịch chạy mỗi giờ nếu đang bật sẽ **tự** trỏ sang bản
mới — không phải chạy lại `schedule`.

Không cần chạy `insight setup` lần nữa, trừ khi `status` báo còn thiếu gì đó.

**Có một thứ đã đổi, và nên biết.** Dữ liệu sử dụng cục bộ trước đây đến từ OTel
span exporter của Copilot, phải bật thủ công trên từng máy qua các setting
`github.copilot.chat.otel.*` trong VS Code. Bây giờ nó đến từ session journal của
chính Copilot CLI tại `~/.copilot`, thứ được ghi ra dù có ai đang đọc hay không.
Nếu máy bạn còn các setting đó, `insight setup` sẽ **gỡ chúng đi** — một exporter
đã nghỉ hưu mà vẫn bật thì vẫn tiếp tục ghi prompt vào một file không ai đọc.

**Không còn `--token` nữa.** Nếu trước đây bạn từng được gửi một khoá qua chat,
`insight rotate-token` thay nó bằng một khoá chưa từng đi đâu cả.

---

## 1 · Hai lệnh

```bash
curl -fsSL https://aeris-insight.seta-international.com/install | sh
insight setup
```

Lệnh đầu cài một zipapp Python chỉ dùng thư viện chuẩn: một launcher tại
`~/.local/bin/insight` và phần archive nằm dưới `~/.local/share/seta-insight/`.
Không có gì được ghi vào `~/.seta-insight/` — đó là việc của `insight setup`.
Lệnh thứ hai là một cuộc hỏi đáp ngắn — bốn câu, khoảng một phút:

```
$ insight setup

This sets up collection on this machine. Four questions.

This collects, from this machine:

  - which platform agent ran, on which ticket, and how long it took
  - Copilot token counts, premium requests, model ids
  - commit hashes, line counts, and AI provenance markers

It never collects prompts, responses, source code, diffs, file contents, or
secrets. Counts, hashes and fixed categories only.

**This machine will upload on its own, every hour.** ...

Collect telemetry from this machine? [y/N] y

Your work email: ban@seta-international.vn

Collect and upload once an hour, automatically? [Y/n] y

Copilot has worked in 7 repositories on this machine.
A commit hook can stamp which agent run produced each commit.
Without it, cost per accepted output cannot be computed.
Install it? [y/N] y
```

Thêm `--dry-run` nếu muốn xem trước nó sẽ đổi gì mà không ghi gì. Còn
`--email ban@seta-international.vn --yes` bỏ qua các câu hỏi, dành cho ai muốn
script hoá.

**Không phải đăng ký repo bằng tay.** Copilot ghi lại git root của mọi session nó
mở, nên `scan` tự tìm ra những repo bạn thực sự làm việc — con số "7 repositories"
ở trên từ đó mà ra. Trước đây có cờ `--repo` phải lặp lại, và cách nó hỏng là hỏng
về cấu trúc: repo bạn quên khai chính là repo âm thầm không báo gì, và một bundle
thiếu nó trông y hệt một tuần không làm gì. `scan --repo` và `install-hook --repo`
vẫn nhận đường dẫn cụ thể khi bạn cần.

`setup` cấu hình VS Code, ghi nhận sự đồng ý của bạn, và cài commit hook nếu bạn
đồng ý. Nó backup `settings.json`, **giữ nguyên mọi setting bạn đang có**, và
**khôi phục backup nếu kết quả không parse được** — nên file của bạn hoặc đúng,
hoặc không bị đụng tới. Làm tay việc này đã sai 3 lần trong một buổi chiều ở đây,
và **không lần nào báo lỗi cả**.

Sau đó **thoát hẳn VS Code rồi mở lại** (không phải Reload Window).

### Trình cài đặt chỉ cài, rồi dừng

Nó không chạy `setup` giúp bạn. Nhìn thì tưởng là thiếu sót, nên nói rõ vì sao
không phải.

Đưa một script qua ống vào `sh` nghĩa là **không có terminal nào gắn vào cả**.
Trong tình huống đó `setup` không hỏi được bạn câu nào, nên chạy nó từ trình cài
đặt đồng nghĩa với chạy kèm `--yes` — tức là ghi nhận một quyết định đồng ý mà
chẳng ai được xem. Sự đồng ý có được nhờ một cái cờ trên đường ống thì không phải
là sự đồng ý.

Và dòng whitelist mà `setup` in ra ở cuối là thứ duy nhất ở đây bắt buộc một con
người phải đọc và làm gì đó. Chữ nằm ở đuôi một trình cài đặt chạy qua ống là chữ
người ta lướt qua. Tách làm hai lệnh đặt nó trước mặt người đang thực sự nhìn.

### Nếu bạn không muốn đưa script thẳng vào `sh`

`curl | sh` là *tin tưởng ngay lần đầu* trên nền TLS: bạn tin rằng host đúng là
thứ DNS và chứng chỉ nói, và thứ nó phục vụ đúng là thứ chúng tôi phát hành. Đó
là một giả định thật, đáng viết hẳn một đoạn chứ không phải một dòng chú thích.

Cách hai bước cho bạn kiểm tra script trước khi nó chạy:

```bash
curl -fsSL -o install.sh https://aeris-insight.seta-international.com/install
shasum -a 256 install.sh
# so với digest công bố bên dưới, rồi:
sh install.sh
```

> **Digest công bố của `install.sh`:**
> `PLACEHOLDER — sinh ra lúc phát hành, điền vào khi cắt artifact`

Dù chọn cách nào, bản thân script cũng tự xác minh thứ nó tải về: artifact
`insight.pyz` lấy từ GitHub Releases của repo công khai `seta-canhta/usage-insight`
và được đối chiếu với một SHA-256 nhúng sẵn trong script. **Lệch checksum thì nó
từ chối cài, chứ không cài một thứ chưa được xác minh.** Hãy đọc script — nó ngắn,
và đọc nó chính là ý nghĩa của cách hai bước.

### Một dòng cần gửi đi

Kết thúc `setup`, nó in ra một dòng như thế này:

```
    ban@seta-international.vn:9f2ac41e8b...c3d1
```

**Gửi dòng đó cho người vận hành pipeline.** Nó được thêm vào danh sách cho phép
trên server, và `insight ship` sẽ từ chối upload cho tới khi dòng đó có mặt.

Dòng đó là *hash*, không phải mật khẩu — dán vào chat cũng an toàn. Khoá bí mật
thật được tạo ngay trên máy bạn, nằm trong `~/.seta-insight/config.json`, và
không bao giờ được gửi cho ai, kể cả người quản lý danh sách cho phép.

Mất rồi? `insight whoami` in lại.

**Không còn cách nào để được *cấp* một khoá nữa,** và đó là cố ý. Cờ
`setup --token` cũ cho phép admin tạo khoá rồi gửi cho bạn, để chuẩn bị sẵn cho
người mới trước cả khi họ chạm vào laptop. Nó cũng đẩy một khoá còn sống đi qua
Slack. Bỏ cờ đó là bỏ luôn ngoại lệ: từ nay mọi khoá trên mọi máy đều là khoá
chưa từng đi đâu. Cái giá phải trả: không ai được chuẩn bị sẵn trước khi tự chạy
`setup` — mỗi engineer tự tạo khoá trên máy mình rồi gửi dòng `email:fingerprint`.

Email của bạn chỉ dùng cho một việc: cho server biết ai đang upload, để tuần nào
thiếu dữ liệu thì hỏi đúng người. **Nó không bao giờ được ghi vào bundle** — dữ
liệu thu thập không chứa địa chỉ email nào cả.

## 2 · Biết rõ máy bạn đang giữ gì, và cái gì rời khỏi máy

Copilot CLI ghi nhật ký từng phiên tại
`~/.copilot/session-state/<session-id>/events.jsonl`. Nó ghi để phục vụ `/resume`
của chính nó, dù có ai đang đọc hay không, và nó không gửi file đó đi đâu cả.

**File đó phần lớn là nội dung.** Prompt của bạn, câu trả lời của Copilot, nội
dung file, output của mọi lệnh nó chạy, và đường dẫn tuyệt đối có kèm username của
bạn — tất cả nằm ngay cạnh các con số. Đó là file của bạn. Hãy coi nó như ghi chú
riêng.

Thứ bảo vệ phần dữ liệu **rời khỏi máy bạn** là: bộ đọc **gọi tên những trường nó
giữ lại**, chứ không phải liệt kê những trường cần loại:

- Tổng cộng 67 trường có tên, theo đường dẫn chính xác, trên 16 loại bản ghi.
  Mọi thứ khác bị bỏ đi mà không hề được đọc tới. Một danh sách loại trừ chỉ tốt
  ngang hiểu biết hôm nay về một định dạng file có thể thêm khoá mới bất cứ lúc nào.
- Mọi giá trị có cấu trúc bị từ chối thẳng — chữ tự do hay nấp trong cấu trúc.
- Ba thứ được đọc để **phân loại** rồi vứt đi: câu lệnh shell (đây là cổng kiểm
  tra nào?), thông báo lỗi của tool (thuộc loại hỏng nào?), và phần đuôi output
  của lệnh (mã thoát là bao nhiêu?). Không thứ nào trong ba thứ đó được lưu.
- Đường dẫn file được đổi thành tương đối so với repo. Đường dẫn không nằm dưới
  repo nào bạn làm việc thì bị **bỏ**, chứ không bị cắt ngắn.
- Trước khi ghi bất cứ thứ gì, mọi sự kiện được kiểm lại một lần nữa với chính
  danh sách cho phép của collector, và cả lần đọc bị từ chối nếu có một trường
  nằm ngoài danh sách.

Đo ngày 2026-08-26 trên 22 journal thật: sinh ra 2.935 sự kiện, và **không một
đường dẫn tuyệt đối, không một username nào** trong kết quả.

**`insight copilot` không bao giờ xoá journal của bạn.** Lệnh mà nó thay thế có
xoá rỗng file span của Copilot sau mỗi lần đọc, vì file đó tồn tại chỉ do chúng
tôi yêu cầu. File này thì khác: nó là lịch sử công việc của chính bạn theo cách
Copilot ghi, và là thứ `/resume` đọc lại. Đọc lại nhiều lần cũng an toàn: sự kiện
được đánh khoá sao cho một phiên đọc mỗi giờ suốt một tuần vẫn chỉ lưu một lần.

### Nó không nhìn thấy gì

Journal chỉ bao phủ **bề mặt Copilot CLI và agent**. Panel Chat trong VS Code và
inline completion không ghi gì vào đó. Nếu bạn làm việc chủ yếu trong panel Chat
thì phần lớn lượng dùng AI của bạn hiện **không được đo**.

Đó là một **giới hạn**, không phải một kết quả, và mỗi lần chạy đều nói ra:

```json
"coverage": {"sessions": 22, "sessions_with_usage": 20,
             "sessions_without_usage": 2, "usage_coverage": 0.909,
             "surfaces_not_covered": ["vscode-copilot-chat", "inline-completions"]}
```

`sessions_without_usage` là nửa còn lại của cùng một sự trung thực. Một phiên kết
thúc không sạch — crash, bị kill, hoặc vẫn đang mở — không ghi lại tổng lượng dùng
nào cả. Token của nó là **không thể biết, không phải bằng không**, và phía sau
không được phép hiển thị nó thành `0`.

## 3 · Nối commit với agent đã tạo ra nó

`setup` sẽ hỏi và cài giúp cho mọi repo Copilot đã làm việc. Muốn thêm về sau:

```bash
cd ~/path/to/qa-automation
insight install-hook --repo .
```

Nó thêm một dòng `AI-Run-Id: run_abc123` vào **cuối** commit message khi có agent
đang chạy. Nó không đụng vào dòng tiêu đề commit, và **không bao giờ làm hỏng
commit** — có lỗi gì thì nó im lặng bỏ qua.

Đây là thứ duy nhất mà việc tự tìm repo không thay thế được: dòng trailer đó là
bằng chứng duy nhất nối một commit với một lần chạy agent cụ thể, và không có nó
thì **không tính được** chi phí trên mỗi kết quả được chấp nhận.

Nếu bạn đã có sẵn hook `prepare-commit-msg`, nó sẽ từ chối ghi đè. Gặp trường hợp
đó thì báo lại, đừng ép.

---

## Nó tự chạy, mỗi khi bạn dùng Copilot

`setup` cài một hook cho Copilot. Khi Copilot chạy một tool, hook nổ, trả về
trong vài mili-giây, và giao việc thu thập cho tiến trình nền — nhiều nhất mười
phút một lần, nên một giờ bận chỉ tốn một lần chạy, giờ rảnh không tốn gì.

Việc **gửi lên được gom riêng**, nhiều nhất mỗi giờ một lần, và chỉ gửi khi có
thay đổi. Thu thập phải thường xuyên vì bằng chứng hỏng theo thời gian: một thư
mục workspace bị xoá sau khi nhánh merge sẽ mang theo cả lịch sử của nó. Gửi lên
thì không cần, vì một chuỗi bundle gần rỗng chẳng giúp được ai.

| | |
|---|---|
| `insight schedule --status` | đang bật không, chạy lần cuối khi nào |
| `insight schedule --off` | tắt cả hook lẫn timer |
| `~/.seta-insight/auto.log` | mỗi lần chạy một dòng, kể cả lỗi |

Không có gì chạy bằng root, không có gì cài ngoài thư mục home của bạn.

## Nếu bạn muốn tự chạy bằng tay

```bash
insight copilot                                  # session journal của Copilot
insight collect                                  # agent nào, ticket nào
insight scan        # mọi repo Copilot đã làm việc
insight pack --since 2026-08-17 --until 2026-08-23
insight ship        # upload lên
```

`insight setup --no-schedule` để tắt lịch tự động, và khi đó việc nhớ là của bạn.

`copilot` chỉ đọc chứ không xoá: journal của bạn vẫn y nguyên như Copilot để lại,
và chạy lại nó cũng không đếm trùng thứ gì.

`pack` ghi một file trong `~/.seta-insight/.reports/`, còn `ship` upload nó lên.
Cố ý tách làm hai lệnh: **không gì rời khỏi máy bạn cho tới khi bạn gõ lệnh thứ
hai.**

**Cứ mở ra xem trước nếu muốn.** Nó là text thuần: một dòng tóm tắt, rồi mỗi dòng
một sự kiện. Không nén, không giấu gì. `insight ship --dry-run` cho xem sẽ gửi gì
mà không gửi thật.

Chạy `ship` hai lần cũng không sao — server nhận ra bundle nó đã có và báo lại.
Không chắc tuần trước đã gửi được chưa thì cứ chạy lại.

**Tuần không làm gì vẫn phải gửi bundle.** Tuần không có sự kiện là số 0 thật;
tuần không có bundle là *thiếu dữ liệu*. Báo cáo phải phân biệt được hai thứ đó
— vì vậy `pack --since ... --until ...` ghi lại đúng tuần bạn muốn nói tới, kể cả
khi tuần đó không có gì.

### Chạy từ bản clone

Bản cài không phải cách duy nhất. Một bản clone vẫn chạy y như trước, và đó là
cách người ta phát triển tiếp công cụ này:

```bash
git clone git@github.com:seta-canhta/usage-insight.git
cd ~/usage-insight
./insight copilot
```

Cùng một mã nguồn, cùng một hành vi, không có checksum nào phải tin. Mọi thứ
trong tài liệu này đều dùng được với `./insight` thay cho `insight`.

---

## Nâng cấp

Chạy lại đúng một dòng đó:

```bash
curl -fsSL https://aeris-insight.seta-international.com/install | sh
```

Nó thay launcher và archive, không đụng gì khác. Config, machine id, salt, khoá
upload, sự kiện trong bộ đệm và các bundle cũ đều nằm trong `~/.seta-insight/` —
nơi trình cài đặt không bao giờ ghi vào. Lịch chạy mỗi giờ tự trỏ sang bản mới,
nên không phải bật lại gì cả. Chạy `insight status` sau đó để xác nhận.

Đang ở đúng bản mới nhất? Nó báo rồi dừng. `--force` cài đè; `--prefix DIR` cài
vào chỗ khác thay vì `$HOME/.local`.

Với bản clone, nâng cấp là `git pull`.

## Gỡ cài đặt

```bash
curl -fsSL -o install.sh https://aeris-insight.seta-international.com/install
sh install.sh --uninstall
```

Lệnh đó gỡ launcher, archive và lịch chạy mỗi giờ. Nó **cố ý** để lại ba thứ, vì
xoá âm thầm sẽ là mặc định sai:

1. **Các commit hook.** Tự xoá `prepare-commit-msg` trong `.git/hooks/` của từng
   repo — trình cài đặt không bao giờ sửa một repo thuộc về bạn.
2. **Dữ liệu bạn đã thu thập.** `insight purge --yes` xoá mọi sự kiện và bundle
   trên máy này; `insight purge --yes --all` xoá luôn cả config và khoá upload,
   tức quên hẳn máy này. Hãy chạy **trước khi** gỡ nếu bạn muốn xoá, vì sau đó
   lệnh không còn ở đó nữa.
3. **Session journal của chính Copilot.** Chúng chưa bao giờ là của chúng tôi;
   `~/.copilot` được để nguyên.

Xoá dòng của bạn khỏi whitelist trên server là việc phải nhờ người vận hành
pipeline. Bundle đã upload thì đã upload rồi.

---

## Quyền của bạn

| | |
|---|---|
| `insight status` | đang có gì trong bộ đệm, và đã upload những gì |
| `insight ship --dry-run` | sẽ gửi gì, mà không gửi thật |
| `insight whoami` | in lại dòng cho danh sách cho phép |
| `insight rotate-token` | đổi khoá upload (upload vẫn chạy bình thường) |
| `insight purge --yes` | xoá sạch mọi sự kiện và bundle trên máy này |
| `insight purge --yes --all` | xoá luôn, và quên hẳn máy này |
| Tắt hoàn toàn | `insight schedule --off`, rồi `insight purge --yes --all` |

`purge` chỉ xoá trên máy bạn. Bundle đã upload thì đã upload rồi — cần xoá thì
hỏi người vận hành pipeline.

### Nếu cài đặt thất bại

| bạn thấy gì | nghĩa là gì |
|---|---|
| *python3 not found* | macOS: `xcode-select --install` sẽ có sẵn một bản. Ubuntu: `sudo apt-get install -y python3`. Trình cài đặt in ra đúng gợi ý cho hệ điều hành của bạn chứ không tự đoán một interpreter khác |
| *python3 is too old* (< 3.9) | mọi bản `python3` nó tìm thấy đều cũ hơn mức client hỗ trợ. Cài bản mới hơn rồi chạy lại, hoặc chỉ đích danh bằng `INSIGHT_PYTHON=/path/to/python3 sh install.sh`. Nó sẽ không âm thầm dùng bản không được hỗ trợ |
| *checksum mismatch* | thứ tải về không phải thứ chúng tôi phát hành. **Không có gì được cài.** Đừng cố chạy tiếp — báo cho người vận hành pipeline. Hoặc là tải hỏng, hoặc là có chuyện cần xem lại |
| `insight: command not found` sau khi cài thành công | `~/.local/bin` chưa nằm trong `PATH`. Thêm `export PATH="$HOME/.local/bin:$PATH"` vào `~/.zshrc` (macOS) hoặc `~/.bashrc` (Ubuntu) rồi mở terminal mới. Trình cài đặt cũng in dòng này ở cuối |

### Nếu upload thất bại

`ship` sẽ nói rõ là trường hợp nào, và không trường hợp nào làm mất bundle —
nó vẫn nằm trong `~/.seta-insight/.reports/` và lần chạy sau sẽ gửi lại.

| thông báo | nghĩa là gì |
|---|---|
| *not on the whitelist* (`401`) | dòng của bạn chưa tới server, hoặc bạn vừa đổi khoá và khoá cũ đã hết hiệu lực. Chạy `insight whoami` để in lại |
| *did not reach the endpoint* | một thứ đứng trước server — CDN hoặc proxy — đã chặn request. Máy bạn không cần sửa gì; báo cho người vận hành pipeline |
| *a certificate this machine cannot verify* | Python trên máy bạn không có chứng chỉ CA. Với bản cài từ python.org trên macOS, chạy `/Applications/Python 3.x/Install Certificates.command` |
| *unreachable after 3 attempts* | mạng, hoặc server đang tắt. Lần chạy theo giờ kế tiếp sẽ gửi lại |

## Đây không phải cái gì

Các con số này mô tả **một cách làm việc đang diễn ra thế nào** — việc có AI hỗ
trợ có được duyệt ngay lần đầu không, test có thực sự chạy không, một phiên tốn
bao nhiêu.

Nó **không phải hồ sơ đánh giá cá nhân**, và không đủ cơ sở để đánh giá ai cả.
Việc thu thập là tự nguyện, bạn đọc được mọi thứ trước khi nó rời máy bạn, bạn tắt
được lịch tự chạy bất cứ lúc nào, và xoá được thứ đang giữ ở đây. Điều đó là cố ý
— và cũng chính là lý do dữ liệu này không bao giờ có thể dùng làm bằng chứng
kiểm tra ai đó.

Có thắc mắc, hoặc thấy gì đó lạ trong bundle: hỏi trước khi gửi.
