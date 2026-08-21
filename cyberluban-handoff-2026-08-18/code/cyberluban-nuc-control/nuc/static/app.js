(() => {
  "use strict";

  const networkBadge = document.querySelector("#networkBadge");
  const serialBadge = document.querySelector("#serialBadge");
  const controlBadge = document.querySelector("#controlBadge");
  const movementValue = document.querySelector("#movementValue");
  const sprayValue = document.querySelector("#sprayValue");
  const tokenInput = document.querySelector("#tokenInput");
  const claimButton = document.querySelector("#claimButton");
  const activateButton = document.querySelector("#activateButton");
  const sprayOnButton = document.querySelector("#sprayOnButton");
  const sprayOffButton = document.querySelector("#sprayOffButton");
  const emergencyButton = document.querySelector("#emergencyButton");
  const messageBox = document.querySelector("#messageBox");
  const movementButtons = [...document.querySelectorAll("[data-movement]")];

  const movementNames = { w: "前进", s: "后退", a: "左转", d: "右转" };
  const heldMovementKeys = [];
  let socket = null;
  let reconnectTimer = null;
  let isController = false;
  let authenticated = false;
  let desiredMovement = null;
  let desiredSpray = false;

  tokenInput.value = localStorage.getItem("cyberluban-control-token") || "";

  function setMessage(message, type = "") {
    messageBox.textContent = message;
    messageBox.className = `message-box ${type}`.trim();
  }

  function setBadge(element, text, on) {
    element.textContent = text;
    element.className = `badge ${on ? "badge-on" : "badge-off"}`;
  }

  function send(payload) {
    if (!socket || socket.readyState !== WebSocket.OPEN) return false;
    socket.send(JSON.stringify(payload));
    return true;
  }

  function sendControl(type = "control") {
    if (!isController) return;
    send({ type, movement: desiredMovement, spray: desiredSpray });
    renderDesiredState();
  }

  function renderDesiredState() {
    movementValue.textContent = desiredMovement ? movementNames[desiredMovement] : "停止";
    sprayValue.textContent = desiredSpray ? "开启" : "关闭";
    sprayValue.style.color = desiredSpray ? "var(--water)" : "";
    movementButtons.forEach((button) => {
      button.classList.toggle("active", button.dataset.movement === desiredMovement);
    });
  }

  function resetLocalState() {
    heldMovementKeys.length = 0;
    desiredMovement = null;
    desiredSpray = false;
    renderDesiredState();
  }

  function emergencyStop(reason = "已发送强制停止") {
    resetLocalState();
    send({ type: "emergency" });
    setMessage(reason, "error");
  }

  function emergencyStopKeepalive() {
    resetLocalState();
    const token = tokenInput.value.trim();
    fetch("/api/emergency-stop", {
      method: "POST",
      headers: { "X-Control-Token": token },
      keepalive: true,
    }).catch(() => {});
  }

  function claimControl() {
    const token = tokenInput.value.trim();
    localStorage.setItem("cyberluban-control-token", token);
    if (!send({ type: "claim", token })) {
      setMessage("网页尚未连接 NUC，请稍后重试。", "error");
    }
  }

  function requestActivation() {
    if (!isController) {
      setMessage("请先取得控制权，再执行激活/校准。", "error");
      return;
    }
    resetLocalState();
    if (send({ type: "activate" })) {
      setMessage("正在发送安全急停和校准请求，请等待 GESTURE COMPLETE。", "success");
    }
  }

  function connect() {
    clearTimeout(reconnectTimer);
    const scheme = location.protocol === "https:" ? "wss" : "ws";
    socket = new WebSocket(`${scheme}://${location.host}/ws`);

    socket.addEventListener("open", () => {
      setBadge(networkBadge, "网页已连接", true);
      setMessage("已连接 NUC，请输入控制口令并取得控制权。", "success");
      if (tokenInput.value.trim()) claimControl();
    });

    socket.addEventListener("message", (event) => {
      const data = JSON.parse(event.data);
      if (data.type === "result") {
        setMessage(data.message, data.ok ? "success" : "error");
        return;
      }
      if (data.type !== "status") return;

      authenticated = Boolean(data.authenticated);
      isController = Boolean(data.you_are_controller);
      setBadge(
        serialBadge,
        data.serial.connected ? `ESP32 ${data.serial.device}` : "ESP32 未连接",
        Boolean(data.serial.connected),
      );
      controlBadge.textContent = isController
        ? "正在控制"
        : data.controller_active
          ? "控制权被占用"
          : "未取得控制权";
      controlBadge.classList.toggle("active", isController);
      claimButton.disabled = isController;
      claimButton.textContent = isController ? "已取得控制权" : "取得控制权";
      activateButton.disabled = !isController || !data.serial.connected;

      if (!isController) {
        resetLocalState();
      }

      if (!data.serial.connected && data.serial.error) {
        setMessage(`ESP32 串口：${data.serial.error}`, "error");
      }
    });

    socket.addEventListener("close", () => {
      const wasController = isController;
      isController = false;
      authenticated = false;
      resetLocalState();
      setBadge(networkBadge, "网页连接断开", false);
      controlBadge.textContent = "未取得控制权";
      controlBadge.classList.remove("active");
      if (wasController) setMessage("连接中断，NUC 已执行自动急停。", "error");
      reconnectTimer = setTimeout(connect, 1000);
    });

    socket.addEventListener("error", () => socket.close());
  }

  function pressMovement(key) {
    if (!isController) return;
    const existingIndex = heldMovementKeys.indexOf(key);
    if (existingIndex >= 0) heldMovementKeys.splice(existingIndex, 1);
    heldMovementKeys.push(key);
    desiredMovement = key;
    sendControl();
  }

  function releaseMovement(key) {
    const index = heldMovementKeys.indexOf(key);
    if (index >= 0) heldMovementKeys.splice(index, 1);
    desiredMovement = heldMovementKeys.at(-1) || null;
    sendControl();
  }

  document.addEventListener("keydown", (event) => {
    if (event.key === " " && authenticated) {
      event.preventDefault();
      emergencyStop("空格急停：运动和喷水均已停止。");
      return;
    }
    if (event.target instanceof HTMLInputElement) return;
    const key = event.key.toLowerCase();

    if (["w", "s", "a", "d", "g", "k", "l", " "].includes(key)) {
      event.preventDefault();
    }
    if (event.repeat && ["w", "s", "a", "d", "g"].includes(key)) return;

    if (["w", "s", "a", "d"].includes(key)) {
      pressMovement(key);
    } else if (key === "g") {
      requestActivation();
    } else if (key === "k" && isController) {
      desiredSpray = true;
      sendControl();
    } else if (key === "l" && isController) {
      desiredSpray = false;
      sendControl();
    }
  });

  document.addEventListener("keyup", (event) => {
    if (event.target instanceof HTMLInputElement) return;
    const key = event.key.toLowerCase();
    if (["w", "s", "a", "d"].includes(key)) {
      event.preventDefault();
      releaseMovement(key);
    }
  });

  movementButtons.forEach((button) => {
    const key = button.dataset.movement;
    button.addEventListener("pointerdown", (event) => {
      event.preventDefault();
      button.setPointerCapture(event.pointerId);
      pressMovement(key);
    });
    const release = (event) => {
      event.preventDefault();
      releaseMovement(key);
    };
    button.addEventListener("pointerup", release);
    button.addEventListener("pointercancel", release);
    button.addEventListener("lostpointercapture", () => releaseMovement(key));
  });

  claimButton.addEventListener("click", claimControl);
  activateButton.addEventListener("click", requestActivation);
  tokenInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter") claimControl();
  });
  sprayOnButton.addEventListener("click", () => {
    if (!isController) return;
    desiredSpray = true;
    sendControl();
  });
  sprayOffButton.addEventListener("click", () => {
    if (!isController) return;
    desiredSpray = false;
    sendControl();
  });
  emergencyButton.addEventListener("click", () => {
    if (authenticated) emergencyStop("按钮急停：运动和喷水均已停止。");
  });

  window.addEventListener("blur", () => {
    if (isController) emergencyStop("页面失去焦点，已自动急停。");
  });
  document.addEventListener("visibilitychange", () => {
    if (document.hidden && isController) emergencyStopKeepalive();
  });
  window.addEventListener("pagehide", () => {
    if (isController) emergencyStopKeepalive();
  });

  setInterval(() => {
    if (isController) sendControl("heartbeat");
  }, 150);

  renderDesiredState();
  connect();
})();
