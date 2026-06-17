# Đánh giá kiến trúc LangGraph và OpenHands cho pipeline sửa code tự động

## Kết luận tổng thể

Kế hoạch của bạn **đi đúng hướng**, và về mặt kỹ thuật thì hoàn toàn **khả thi** với LangGraph làm lớp orchestration và OpenHands làm lớp thực thi sửa code. LangGraph đã có sẵn các primitive rất hợp với sơ đồ của bạn: orchestrator-worker, execution song song, checkpoint/persistence, interrupt để human-in-the-loop, cùng timeout/retry ở mức node. OpenHands thì lại đúng “miền vấn đề” cho worker code: chạy lệnh, sửa file, chạy test, sandbox Docker, và còn có cả mô hình sub-agent, security/confirmation, observability, metrics và workflow code review. citeturn8view0turn8view1turn8view2turn17view0turn8view3turn14view0turn22view1turn21search1

Tuy nhiên, nếu mục tiêu là **production-grade coding system**, mình không khuyên giữ nguyên sơ đồ hiện tại theo kiểu “nhiều committee ở mọi tầng”. Lý do là: multi-agent thường mạnh nhất ở các bài toán **đọc, tìm kiếm, phân tách thông tin, và song song hóa bề rộng**, còn các bài toán **viết code** thường ít phần thật sự song song hơn, phụ thuộc ngữ cảnh nhiều hơn, và chi phí phối hợp giữa agent dễ vượt lợi ích. Anthropic mô tả rõ rằng multi-agent hữu ích cho research/breadth-first search, nhưng “most coding tasks” có ít phần việc song song thật sự hơn; LangChain cũng nhấn mạnh hệ multi-agent thiên về “read” thường dễ hơn “write”; còn một nghiên cứu năm 2026 cho thấy khi chuẩn hóa ngân sách reasoning tokens, single-agent có thể ngang hoặc hơn nhiều kiến trúc multi-agent trên các bài reasoning nhiều bước. citeturn12view0turn13view0turn10view0

Vì vậy, đánh giá thực tế nhất là: **giữ multi-agent ở pha đọc, làm rõ bài toán, lập kế hoạch, phản biện, review; nhưng giữ pha viết là single-writer**. Nói cách khác, sơ đồ của bạn nên trở thành kiến trúc **multi-reader, single-writer**. Đây là điểm quan trọng nhất nếu bạn muốn hệ thống vừa mạnh vừa ổn định. Nhận định này là suy luận từ việc Anthropic cảnh báo về độ khó của “collaborative writing”, từ việc OpenHands coi chạy song song là tính năng còn experimental và nêu rõ rủi ro race condition/ghi vào cùng file, và từ taxonomy failure modes của các hệ MAS cho thấy lỗi phối hợp và xác minh là nguồn thất bại rất phổ biến. citeturn13view0turn19view0turn9view2

## Những phần của kế hoạch đang rất đúng

Phần mạnh nhất trong thiết kế của bạn là việc đặt một **LangGraph Orchestrator** ở trên cùng để quản state, routing, budget/retry và policy gate. Đây gần như là “fit tự nhiên” với LangGraph: graph state có thể được checkpoint theo thread, resume sau interrupt, và hỗ trợ fault tolerance, timeouts, retries cùng execution metadata như thread ID, run ID và attempt number. Với dạng pipeline nhiều pha và có loop rework như sơ đồ của bạn, đây là chỗ LangGraph tỏ ra phù hợp hơn các loop agent tối giản. citeturn8view1turn8view2turn20view0turn2search13

**Parallel Intake Committee** cũng là ý hay. Việc tách riêng một agent hiểu yêu cầu, một agent tìm ambiguity/edge cases, và một agent lấy repo context là hợp lý vì dưới góc nhìn nghiên cứu mới hơn, các issue mơ hồ hoặc under-specified là nguyên nhân thực tế dẫn tới patch “plausible but wrong”. Một nghiên cứu ICSE 2026 về SWE-bench cho thấy ngay cả benchmark đã được lọc vẫn còn vấn đề issue statement chưa đủ rõ, và chính tác giả nhấn mạnh nhu cầu có các tool biết phát hiện và tinh chỉnh các yêu cầu mơ hồ. Vì thế, “Intake Agent B” của bạn không phải đồ trang trí; đây là vai trò có ROI cao. citeturn23view0turn24view0

**Intake Synthesizer / Arbiter** cũng là phần đúng bản chất. Anthropic mô tả rằng để subagent làm việc tốt, mỗi agent cần objective rõ, output format rõ, hướng dẫn tool/source rõ, và boundary rõ; nếu không thì agent sẽ trùng lặp, bỏ sót hoặc hiểu sai việc. Điều đó gần như là lập luận trực tiếp cho việc cần một lớp synthesize/arbitrate trước khi đi sang planning. Nói cách khác, bạn không nên để ba intake agent đẩy thẳng vào coding; phải có một lớp hợp nhất thành **Problem Statement chuẩn hóa**. citeturn13view0turn12view0

**Parallel Planning Committee** và **Plan Debate / Critic Layer** cũng có cơ sở. OpenHands đã có ví dụ chính thức cho workflow hai giai đoạn: planning agent với tool read-only để phân tích và tạo plan, rồi execution agent mới có quyền edit. Ngoài ra, OpenHands còn có “Critic” chạy song song với agent để ước lượng xác suất thành công và kích hoạt iterative refinement, dù họ gắn nhãn tính năng này là experimental. Tức là bản thân hệ sinh thái OpenHands cũng thừa nhận rằng “plan trước, critique sau, rồi mới execute” là pattern hợp lệ. citeturn14view4turn14view1

Cuối cùng, việc để **OpenHands Coding Worker** đảm nhận sửa code, chạy command, chạy test, và trả về patch/logs là lựa chọn tốt. OpenHands là agent system được thiết kế xoay quanh code, tools và workspace; agent của họ là stateless theo từng step, event-driven, interruptible, và Docker sandbox là lựa chọn mặc định được khuyến nghị để cô lập host. Họ cũng có sẵn use case PR review qua GitHub Actions, nên chuỗi “worker → review → rework → report” của bạn rất hợp với năng lực gốc của nền tảng này. citeturn14view0turn8view3turn27view0turn8view6turn21search10

## Những rủi ro cốt lõi cần sửa trước khi xây thật

Rủi ro lớn nhất của sơ đồ hiện tại là **committee inflation**: càng nhiều planner, critic, reviewer thì càng tăng token, latency và chỗ để sai lệch ngữ cảnh. Anthropic báo cáo multi-agent thường tiêu tốn token cao hơn đáng kể so với tương tác chat hay single-agent, và nhấn mạnh rằng tính kinh tế chỉ hợp lý khi giá trị nhiệm vụ đủ cao. Cùng lúc đó, nghiên cứu về failure modes của multi-agent systems đã chỉ ra 14 failure modes thuộc ba nhóm lớn: lỗi đặc tả/hệ thống, lệch pha giữa agent, và lỗi xác minh/kết thúc. Nói ngắn gọn: thêm agent không chỉ thêm “trí tuệ”, mà còn thêm **coordination overhead**. citeturn12view0turn12view1turn9view2

Rủi ro thứ hai là **parallel fan-out nhưng không có state contract rõ ràng**. Trong LangGraph, nếu nhiều node song song cùng ghi vào một state key mà key đó không có reducer hỗ trợ concurrent updates, bạn sẽ dính lỗi `INVALID_CONCURRENT_GRAPH_UPDATE`. Vì vậy, phần Intake Committee, Planning Committee, và Review Committee của bạn không thể chỉ là “ba agent trả text rồi ghép lại”; bạn phải định nghĩa state kiểu append-only hoặc reducer rõ ràng cho các collection như `intake_findings`, `candidate_plans`, `review_findings`. Nếu không, càng song song càng dễ vỡ orchestration. citeturn8view0turn16view0

Rủi ro thứ ba là **cho nhiều agent cùng ghi code**. May là sơ đồ của bạn hiện mới có một OpenHands Coding Worker, và đây là điều nên giữ. OpenHands ghi rất rõ parallel tool execution còn experimental, mặc định concurrency là `1`, và concurrent execution có thể tạo race condition hoặc hành vi bất ngờ khi tools cùng chia sẻ state, nhất là khi ghi vào cùng file hay phụ thuộc thứ tự. LangChain cũng nhấn mạnh rằng multi-agent “write” khó hơn “read” vì cần truyền ngữ cảnh quyết định và merge kết quả nhất quán. Từ các nguồn này, mình đánh giá: **đừng mở rộng schema này thành nhiều coding worker cùng sửa một workspace/branch** trừ khi bạn tách worktree/branch hoàn toàn và có cơ chế merge rất chặt. citeturn19view0turn13view0

Rủi ro thứ tư là **tin rằng test pass là đủ**. Thực tế không phải vậy. Bài ICSE 2026 cho thấy ngay cả với SWE-bench Verified, có những patch được tính là “đúng” dù fail khi chạy đủ developer tests, và nhiều patch dù “plausible” vẫn khác hành vi so với patch gốc của developer. Điều này có hai hệ quả cho sơ đồ của bạn: một là review layer không thể chỉ nhìn “tests green”; hai là acceptance criteria phải bao gồm cả regression review, semantic diff review, và khi cần là human approval cho case rủi ro cao. citeturn23view0turn24view0

Rủi ro thứ năm là **repo-context intake biến thành prompt-injection intake**. Đây là điểm rất nhiều team bỏ sót. OWASP coi prompt injection là rủi ro hàng đầu cho hệ GenAI/LLM. Trong khi đó, OpenHands skill system có thể parse `AGENTS.md`, `agents.md`, `.cursorrules`, thậm chí repository skills còn có thể kèm MCP tool config và dynamic content rendering bằng inline commands. Điều đó cực mạnh về mặt repo customization, nhưng cũng có nghĩa là nếu bạn nạp context từ nhánh PR không tin cậy hoặc từ file do attacker kiểm soát, bạn có thể vô tình cho agent học chỉ dẫn độc hại hoặc mở tool ngoài ý muốn. Vì vậy, **repo-context intake phải có trust boundary**: chỉ tin file hướng dẫn từ base branch/allowlist, không tự động tin mọi chỉ dẫn trong PR head. citeturn4search0turn25view0

## Kiến trúc đề xuất tối ưu hơn

Nếu giữ tinh thần thiết kế hiện tại nhưng muốn thực chiến hơn, mình khuyên đổi từ “nhiều committee mọi chỗ” sang **kiến trúc phân tầng có hợp đồng dữ liệu rõ và một writer duy nhất**. Dạng này vẫn bám sát ý tưởng ban đầu của bạn, nhưng cắt bớt phần thừa và harden các điểm dễ hỏng. LangGraph hỗ trợ rất tốt kiểu orchestrator-worker với fan-out/fan-in, còn OpenHands phù hợp để nhận task spec cuối cùng rồi thực thi trong sandbox. citeturn8view0turn8view1turn18search0

Cấu trúc mình đề xuất là như sau:

```text
User Task
  -> Preflight / Repo Snapshot
  -> Read-only Intake Committee
       - User Intent
       - Ambiguity & Edge Cases
       - Trusted Repo Context
  -> Intake Synthesizer
       - Problem Statement
       - Repro / Constraints / Risk Class
  -> Read-only Planning Committee
       - Minimal Plan
       - Robust Plan
       - Test-first Plan
  -> Critique Layer
       - Risk
       - Test Coverage
       - Security / Regression
  -> Plan Arbiter
       - Final Plan
       - Acceptance Criteria
       - Worker Task Spec
  -> Human Gate for High-risk Tasks
  -> Single OpenHands Coding Worker
  -> Automated Review Stack
       - Full Tests
       - Diff Review
       - Security / Regression Review
  -> Bounded Rework Loop
  -> Reporter
```

Điểm khác biệt đáng kể ở đây là thêm **Preflight / Repo Snapshot** ngay đầu pipeline. Stage này nên chốt commit SHA, branch/workspace, baseline test status, repro command nếu có, và risk class của issue trước khi bất kỳ committee nào bắt đầu suy luận. Điều này làm giảm tranh cãi vô ích ở các tầng sau và giúp replay/debug được về sau. Anthropic và LangChain đều nhấn mạnh durable execution, observability, và context engineering là nền tảng cho agent dài hơi; còn OpenHands thì có tracing, metrics và event-driven model rất hợp cho artifact hóa stage preflight. citeturn13view0turn22view1turn21search1turn14view0

Mình cũng khuyên **Intake Committee và Planning Committee phải read-only hoàn toàn**. Đây không chỉ là vấn đề “sạch design”, mà còn phù hợp với các ví dụ chính thức của OpenHands về planner read-only rồi mới execution agent có quyền edit. Với cách này, tất cả committee trước worker chỉ được đọc repo, log, docs, config, và trả structured outputs; chỉ worker mới có terminal write/edit quyền cao hơn. Điều này cũng khớp với nguyên lý “multi-agent đọc, single-agent viết” ở trên. citeturn14view4turn13view0turn12view0

Một thay đổi quan trọng nữa là **Code Review Committee nên chia làm automated review stack + human gate chọn lọc**, thay vì coi tất cả reviewer là agent ngang hàng. OpenHands đã có workflow PR review trong GitHub Actions; GitHub lại hỗ trợ protected branches với required status checks và required approving reviews. Vì vậy, thay vì để reviewer agent “có quyền phán quyết tùy ý”, hãy để quyền merge phụ thuộc vào: test checks bắt buộc, review bắt buộc, và human approval cho change class rủi ro cao. Cách này bền hơn rất nhiều so với chỉ dựa vào một committee nội bộ trong graph. citeturn8view6turn7search0turn7search1turn7search4

## Cách hiện thực bằng LangGraph và OpenHands

Ở lớp **LangGraph**, bạn nên tách state thành hai loại. Loại thứ nhất là **thread-scoped state** dùng checkpointer: `problem_statement`, `candidate_plans`, `selected_plan`, `review_findings`, `retry_count`, `risk_class`, `artifacts`. Loại thứ hai là **cross-thread memory/store** cho kiến thức lâu bền như repo conventions, danh sách flaky tests, pattern của bug lặp lại, hoặc allowlist tool policies. LangGraph docs phân biệt rất rõ checkpointer cho short-term/thread memory và store cho long-term/cross-thread memory. citeturn8view1

Các khóa state được fan-out từ committee song song nên dùng reducer append-only. Ví dụ, `intake_findings: Annotated[list, operator.add]`, `candidate_plans: Annotated[list, operator.add]`, `review_findings: Annotated[list, operator.add]`. Đây không phải tối ưu “nice to have”, mà là điều bắt buộc nếu bạn muốn tránh concurrent update errors khi nhiều nhánh song song cùng trả kết quả về orchestrator. citeturn8view0turn16view0

Ở lớp **control**, bạn nên dùng timeout và retry ngay từ đầu. LangGraph hỗ trợ `run_timeout` và `idle_timeout` theo node, và runtime có execution info gồm attempt number để bạn vừa log vừa chặn retry vô hạn. Từ góc nhìn design, điều này khớp hoàn toàn với “budget/retry limit” bạn đặt ở Orchestrator. Nếu một critic/reviewer loop cứ quay mãi, orchestrator phải kết luận “need human escalation” thay vì đốt token vô hạn. citeturn8view2turn20view0

Ở lớp **policy/security gate**, mình khuyên dùng hai tầng. Tầng một ở LangGraph: gate theo risk class của task, ví dụ dependency upgrades, infra config, migration scripts, secret handling, hoặc thay đổi ngoài allowlist path thì phải interrupt/human approval. Tầng hai ở OpenHands: dùng confirmation policy và security analyzer, hoặc ít nhất `ConfirmRisky()` cho worker. LangChain HITL middleware và OpenHands security đều hỗ trợ mô hình “tạm dừng khi có action nhạy cảm, lưu state, rồi resume sau khi được approve/edit/reject”. citeturn17view0turn8view4turn21search6

Ở lớp **repo context**, thay vì để “Repo-context intake” mò mẫm tự do, bạn nên tận dụng cơ chế có sẵn của OpenHands skill system. OpenHands hỗ trợ repository skills, `AGENTS.md`, `CLAUDE.md`, `.cursorrules`, có thể inject context vào prompt, load MCP tools, và parse nhiều định dạng chỉ dẫn. Nhưng vì chính sức mạnh này kéo theo rủi ro prompt injection, hãy chỉ nạp skill/repo instructions từ nguồn đã trust, tốt nhất là từ base branch hoặc allowlist rõ ràng. Với nhánh PR không tin cậy, chỉ nên đọc như dữ liệu, không coi như instruction. citeturn25view0turn4search0turn4search5

Ở lớp **OpenHands worker**, cấu hình tối thiểu nên là Docker sandbox chứ không phải process sandbox, mount đúng workspace cần thiết chứ không mount rộng, và bật secret handling nếu worker có dùng credential. OpenHands docs nêu Docker sandbox là lựa chọn mặc định/khuyến nghị, process sandbox thì “unsafe”; secret registry có thể tự động inject secret dưới dạng environment variables và mask secret trong output. Đây là baseline an toàn tối thiểu nếu worker được phép chạy bash trong repo thật. citeturn27view0turn8view3turn26view0

Ở lớp **context management**, mình khuyên thêm condenser hoặc memory ngoài context cho worker dài hơi. OpenHands có `LLMSummarizingCondenser` để tóm tắt lịch sử khi context dài ra; Anthropic cũng mô tả pattern lưu plan vào memory để tránh mất chiến lược khi context window bị cắt. Với coding task nhiều log/test output, đây là khác biệt lớn giữa một agent “demo được” và một agent làm việc ổn trên issue phức tạp. citeturn22view0turn12view0turn13view0

Ở lớp **observability**, đừng chờ đến lúc vỡ mới thêm tracing. OpenHands hỗ trợ OTEL tracing cho agent steps, tool calls, LLM calls và conversation lifecycle; metrics tracking cho token usage, costs, latency; còn LangChain/LangSmith nhấn mạnh observability/evals là hạ tầng gần như bắt buộc cho agent system. Nếu bạn muốn pipeline này sống được ngoài sandbox demo, phải trace mỗi run theo `task_id`, `thread_id`, `repo_sha`, `risk_class`, `selected_plan`, `worker_attempt`, `tests_run`, `review_outcome`. citeturn22view1turn21search1turn13view0

Một điểm cuối ở lớp implementation: **Critic của OpenHands chỉ nên xem là tùy chọn**, không phải nền móng bắt buộc. Nó hữu ích để kích hoạt reflective retry hoặc re-rank trajectory, nhưng bản thân docs gắn nhãn experimental. Vì vậy, với bản đầu tiên, nên để critic ở outer loop của orchestrator như một scorer tham khảo; khi hệ thống ổn hơn mới chuyển thêm quyền cho critic đóng vai trò hard gate. citeturn14view1turn15view0

## Tiêu chí chấp nhận và chỉ số vận hành nên có

Mình sẽ không để “Plan Arbiter” chỉ tạo acceptance criteria chung chung kiểu “tests pass”. Acceptance criteria nên chốt tối thiểu bốn nhóm.

**Nhóm bài toán**: Problem Statement phải có issue/goal, observed vs expected behavior, ambiguity list, phạm vi thay đổi, non-goals, repro hoặc lý do không repro được, và risk class. Đây là cách thực tế để chống lỗi do issue under-specified và lệch vai trò giữa các agent. citeturn23view0turn13view0turn9view2

**Nhóm thực thi**: Worker Task Spec phải nêu allowed paths, forbidden actions, expected files to touch, test commands bắt buộc, definition of done, và điều kiện nào thì escalates to human. Đây là cách biến “arbiter” thành hợp đồng máy-thực-thi được, thay vì một đoạn plan bằng văn xuôi. Nhu cầu task boundary, output format và tool guidance đã được Anthropic nhấn mạnh rất rõ. citeturn13view0turn12view0

**Nhóm chất lượng**: một patch chỉ được coi là đạt khi pass full required status checks, có review findings ở mức blocker bằng 0, và với task bug-fix thì có test mới hoặc test đã cập nhật để tái hiện bug cũ. Đồng thời, bạn nên xem test pass như điều kiện cần chứ không đủ, vì nghiên cứu gần đây cho thấy patch plausible vẫn có thể sai về hành vi. GitHub protected branches hỗ trợ enforce required checks và required reviews rất hợp cho tầng merge gate này. citeturn23view0turn24view0turn7search0turn7search1turn7search13

**Nhóm an toàn vận hành**: Docker sandbox phải là mặc định; risky actions phải đi qua approval; secrets phải được mask; trace và metrics phải đủ để điều tra lại từng run. Nếu bạn pilot hệ thống này, nên bắt đầu bằng một eval set nhỏ nhưng đại diện — LangChain tổng hợp từ thực tiễn agent evaluation rằng thậm chí vài chục datapoints ban đầu cũng đủ để lộ ra pattern lỗi và giúp tuning nhanh hơn. citeturn27view0turn8view4turn26view0turn22view1turn13view0

Nếu cần chốt thành một câu duy nhất: **kiến trúc của bạn tốt ở mức ý tưởng, nhưng chỉ nên productionize theo hướng “song song ở pha đọc và phản biện, một writer duy nhất ở pha sửa code, state contract rõ, review/merge gate cứng, và trust boundary nghiêm khắc cho repo context”**. Đó là phiên bản có khả năng sống thật, thay vì chỉ đẹp trên sơ đồ. citeturn12view0turn13view0turn19view0turn17view0turn7search0