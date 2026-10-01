/* 非终态任务页：轮询事件流，有新事件即刷新页面。
 *
 * 提交表单时停止轮询，避免打断正在进行的 POST（运行/应答/取消）。 */
(function () {
  var container = document.getElementById("events");
  if (!container) {
    return;
  }
  var terminal = container.getAttribute("data-terminal") === "1";
  if (terminal) {
    return;
  }
  var taskId = container.getAttribute("data-task-id");
  var lastSeq = parseInt(container.getAttribute("data-last-seq") || "0", 10);
  var submitting = false;

  document.addEventListener("submit", function () {
    submitting = true;
  });

  function poll() {
    if (submitting) {
      return;
    }
    fetch("/tasks/" + taskId + "/events?after=" + lastSeq)
      .then(function (response) {
        return response.ok ? response.json() : null;
      })
      .then(function (page) {
        if (page && page.items && page.items.length > 0) {
          location.reload();
        } else {
          setTimeout(poll, 2500);
        }
      })
      .catch(function () {
        setTimeout(poll, 5000);
      });
  }

  setTimeout(poll, 1500);
})();
