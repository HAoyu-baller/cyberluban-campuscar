(() => {
  "use strict";

  const networkBadge = document.querySelector("#networkBadge");
  const serialBadge = document.querySelector("#serialBadge");
  const controlBadge = document.querySelector("#controlBadge");
  const movementValue = document.querySelector("#movementValue");
  const sprayValue = document.querySelector("#sprayValue");
  const tokenInput = document.querySelector("#tokenInput");
  const claimButton = document.querySelector("#claimButton");
  const releaseButton = document.querySelector("#releaseButton");
  const activateButton = document.querySelector("#activateButton");
  const sprayOnButton = document.querySelector("#sprayOnButton");
  const sprayOffButton = document.querySelector("#sprayOffButton");
  const visionSprayOnButton = document.querySelector("#visionSprayOnButton");
  const visionSprayOffButton = document.querySelector("#visionSprayOffButton");
  const visionSprayValue = document.querySelector("#visionSprayValue");
  const radarSafetyToggle = document.querySelector("#radarSafetyToggle");
  const radarSafetyValue = document.querySelector("#radarSafetyValue");
  const radarSafetyDetail = document.querySelector("#radarSafetyDetail");
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
  let visionSprayArmed = false;
  let visionSprayController = false;
  let radarSafetyEnabled = false;

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

  function resetMotionState() {
    heldMovementKeys.length = 0;
    desiredMovement = null;
    desiredSpray = false;
    renderDesiredState();
  }

  function emergencyStop(reason = "已发送强制停止") {
    resetMotionState();
    visionSprayArmed = false;
    visionSprayController = false;
    send({ type: "emergency" });
    setMessage(reason, "error");
  }

  function emergencyStopKeepalive() {
    resetMotionState();
    visionSprayArmed = false;
    visionSprayController = false;
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

  function releaseControl() {
    if (!isController) return;
    resetMotionState();
    if (send({ type: "release" })) {
      setMessage("正在停止网页运动并释放控制权；视觉喷水授权不受影响。", "success");
    }
  }

  function setManualSpray(enabled) {
    if (!isController) {
      setMessage("人工喷水需要先取得网页运动控制权。", "error");
      return;
    }
    desiredSpray = Boolean(enabled);
    send({ type: "manual_spray", enabled: desiredSpray });
    renderDesiredState();
  }

  function setVisionSpray(enabled) {
    const token = tokenInput.value.trim();
    if (!token) {
      setMessage("启用视觉喷水前请输入控制口令。", "error");
      return;
    }
    if (send({ type: "spray_arm", enabled: Boolean(enabled), token })) {
      if (!enabled) {
        visionSprayArmed = false;
        visionSprayController = false;
      }
      setMessage(
        enabled
          ? "正在授权视觉喷水；不会取得校园大脑的运动控制权。"
          : "视觉喷水授权已关闭。",
        enabled ? "success" : "",
      );
    }
  }

  function setRadarSafety(enabled) {
    const token = tokenInput.value.trim();
    if (!token) {
      radarSafetyToggle.checked = radarSafetyEnabled;
      setMessage("启用雷达避障前请输入控制口令。", "error");
      return;
    }
    if (!send({ type: "radar_toggle", enabled: Boolean(enabled), token })) {
      radarSafetyToggle.checked = radarSafetyEnabled;
      setMessage("网页尚未连接 NUC，请稍后重试。", "error");
      return;
    }
    setMessage(
      enabled
        ? "正在启用雷达避障保护；确认雷达数据正常后才允许运动。"
        : "正在关闭雷达避障保护；当前运动已停止。",
      enabled ? "success" : "",
    );
  }

  function requestActivation() {
    if (!isController) {
      setMessage("请先取得控制权，再执行激活/校准。", "error");
      return;
    }
    resetMotionState();
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
      setMessage("已连接 NUC；需要人工控制或校准时再取得控制权。", "success");
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
      visionSprayArmed = Boolean(data.spray_authorized);
      visionSprayController = Boolean(data.you_are_spray_controller);
      radarSafetyEnabled = Boolean(data.radar_enabled);
      radarSafetyToggle.checked = radarSafetyEnabled;
      setBadge(
        serialBadge,
        data.serial.connected ? `ESP32 ${data.serial.device}` : "ESP32 未连接",
        Boolean(data.serial.connected),
      );
      controlBadge.textContent = isController
        ? "正在控制"
        : data.ros_control_active
          ? "校园大脑控制中"
        : data.controller_active
          ? "控制权被占用"
          : "未取得控制权";
      controlBadge.classList.toggle("active", isController);
      claimButton.disabled = isController || Boolean(data.ros_control_active);
      claimButton.textContent = isController ? "已取得控制权" : "取得控制权";
      releaseButton.disabled = !isController;
      activateButton.disabled = !isController || !data.serial.ready;
      visionSprayOnButton.disabled = !data.serial.ready || visionSprayArmed;
      visionSprayOffButton.disabled = !visionSprayController;
      visionSprayValue.textContent = visionSprayArmed
        ? visionSprayController
          ? "视觉喷水已授权"
          : "其他页面已授权"
        : "未授权";
      radarSafetyValue.textContent = !radarSafetyEnabled
        ? "已关闭"
        : data.radar_blocked && !data.radar_fresh
          ? "雷达断流，已停车"
          : data.radar_blocked
            ? "检测到障碍，已停车"
            : "保护中";
      radarSafetyDetail.textContent = !radarSafetyEnabled
        ? "当前不拦截运动指令。"
        : data.radar_blocked
          ? `${data.radar_reason || "安全保护触发"}；清除后需重新发送运动指令。`
          : `点云正常，${data.radar_points ?? 0} 个有效点。`;

      if (!isController) {
        resetMotionState();
      }

      if (data.spray_mode === "VISION") {
        sprayValue.textContent = data.spray ? "视觉喷水中" : "视觉等待草坪";
      } else if (data.spray_mode === "MANUAL") {
        sprayValue.textContent = data.spray ? "人工喷水中" : "关闭";
      } else {
        sprayValue.textContent = data.spray ? "开启" : "关闭";
      }

      if (!data.serial.connected && data.serial.error) {
        setMessage(`ESP32 串口：${data.serial.error}`, "error");
      }
    });

    socket.addEventListener("close", () => {
      const wasController = isController;
      isController = false;
      authenticated = false;
      resetMotionState();
      visionSprayArmed = false;
      visionSprayController = false;
      setBadge(networkBadge, "网页连接断开", false);
      controlBadge.textContent = "未取得控制权";
      controlBadge.classList.remove("active");
      radarSafetyValue.textContent = "连接断开";
      radarSafetyDetail.textContent = "无法确认雷达安全状态。";
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
      setManualSpray(true);
    } else if (key === "l" && isController) {
      setManualSpray(false);
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
  releaseButton.addEventListener("click", releaseControl);
  activateButton.addEventListener("click", requestActivation);
  tokenInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter") claimControl();
  });
  sprayOnButton.addEventListener("click", () => {
    setManualSpray(true);
  });
  sprayOffButton.addEventListener("click", () => {
    setManualSpray(false);
  });
  visionSprayOnButton.addEventListener("click", () => setVisionSpray(true));
  visionSprayOffButton.addEventListener("click", () => setVisionSpray(false));
  radarSafetyToggle.addEventListener("change", () => {
    setRadarSafety(radarSafetyToggle.checked);
  });
  emergencyButton.addEventListener("click", () => {
    if (authenticated) emergencyStop("按钮急停：运动和喷水均已停止。");
  });

  window.addEventListener("blur", () => {
    if (isController) emergencyStop("页面失去焦点，已自动急停。");
    else if (visionSprayController) setVisionSpray(false);
  });
  document.addEventListener("visibilitychange", () => {
    if (document.hidden && isController) emergencyStopKeepalive();
    else if (document.hidden && visionSprayController) setVisionSpray(false);
  });
  window.addEventListener("pagehide", () => {
    if (isController) emergencyStopKeepalive();
    else if (visionSprayController) setVisionSpray(false);
  });

  setInterval(() => {
    if (isController) sendControl("heartbeat");
    if (visionSprayArmed && visionSprayController) {
      send({ type: "spray_heartbeat", enabled: true });
    }
  }, 150);

  renderDesiredState();
  connect();
})();
