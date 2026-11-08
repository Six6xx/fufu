/**
 * 17-live2d.js — Live2D 自然行为系统
 * 使用 oh-my-live2d 渲染 Hiyori 模型
 * 通过 hook 内部 update 管线（PIXI ticker 后）覆写参数
 * 实现 idle 自然行为（眨眼、晃动、呼吸、视线）+ TTS 口型同步驱动
 */

const Live2DManager = (() => {
    // ── 内部状态 ──────────────────────────────────────
    let oml2d = null;           // oh-my-live2d 实例
    let coreModel = null;       // Live2D coreModel 引用
    let modelInstance = null;   // pixi-live2d-display 模型实例（有 internalModel 的那个）
    let modelReady = false;     // 模型是否就绪
    let isSpeaking = false;     // 是否正在说话
    let hooked = false;         // 是否已 hook update
    let dragHandle = null;
    let petLayout = null;

    const MODEL_PATH = '/live2d/hiyori_pro_t11.model3.json';

    // ── 眨眼系统 ──────────────────────────────────────
    const blink = {
        nextBlinkTime: 0,
        blinking: false,
        blinkStart: 0,
        blinkDuration: 150,
        doubleBlinkPending: false,
    };

    // ── 身体/头部晃动 ──────────────────────────────────
    const sway = {
        angleX: { phase: 0, speed: 0, amplitude: 3 },
        angleY: { phase: 0, speed: 0, amplitude: 2 },
        angleZ: { phase: 0, speed: 0, amplitude: 1 },
        bodyAngleX: { phase: 0, speed: 0, amplitude: 2 },
        bodyAngleY: { phase: 0, speed: 0, amplitude: 1.5 },
        bodyAngleZ: { phase: 0, speed: 0, amplitude: 0.5 },
    };

    // ── 说话时的额外动作 ─────────────────────────────────
    const speakGesture = {
        // 随机点头
        nodPhase: 0,
        nodSpeed: (2 * Math.PI) / 0.8, // 快速点头周期
        nodActive: false,
        nodEnd: 0,
        nextNodTime: 0,
        // 偶尔挑眉
        browRaise: 0,
        browTarget: 0,
        nextBrowTime: 0,
        // 头部侧倾强调
        tiltTarget: 0,
        tiltCurrent: 0,
        nextTiltTime: 0,
    };

    // ── 呼吸 ──────────────────────────────────────────
    const breath = {
        phase: 0,
        speed: (2 * Math.PI) / 3.5,
    };

    // ── 视线系统 ──────────────────────────────────────
    const gaze = {
        currentX: 0,
        currentY: 0,
        targetX: 0,
        targetY: 0,
        nextGazeTime: 0,
        gazeEndTime: 0,
        isGazing: false,
        lerpSpeed: 0.03,
    };

    // ── 嘴巴 ──────────────────────────────────────────
    let mouthOpenValue = 0;

    // ── 表情叠加 ─────────────────────────────────────
    let emotionParams = {};

    // ── 上一帧时间 ────────────────────────────────────
    let lastTime = 0;

    // ── 工具函数 ──────────────────────────────────────
    function lerp(a, b, t) {
        return a + (b - a) * t;
    }

    function randomRange(min, max) {
        return min + Math.random() * (max - min);
    }

    function now() {
        return performance.now();
    }

    // ── 初始化晃动参数 ────────────────────────────────
    function initSwayParams() {
        sway.angleX.speed = (2 * Math.PI) / randomRange(4, 6);
        sway.angleX.phase = Math.random() * Math.PI * 2;
        sway.angleY.speed = (2 * Math.PI) / randomRange(4.5, 6.5);
        sway.angleY.phase = Math.random() * Math.PI * 2;
        sway.angleZ.speed = (2 * Math.PI) / randomRange(5, 7);
        sway.angleZ.phase = Math.random() * Math.PI * 2;
        sway.bodyAngleX.speed = (2 * Math.PI) / randomRange(6, 9);
        sway.bodyAngleX.phase = Math.random() * Math.PI * 2;
        sway.bodyAngleY.speed = (2 * Math.PI) / randomRange(5, 8);
        sway.bodyAngleY.phase = Math.random() * Math.PI * 2;
        sway.bodyAngleZ.speed = (2 * Math.PI) / randomRange(7, 10);
        sway.bodyAngleZ.phase = Math.random() * Math.PI * 2;
    }

    // ══════════════════════════════════════════════════
    // 核心策略：hook pixi-live2d-display 的 update 管线
    // 在库的 motionManager 更新之后，覆写我们需要控制的参数
    // ══════════════════════════════════════════════════

    /**
     * 深度搜索找到 pixi-live2d-display 模型实例
     * 标志：有 internalModel 属性（不再要求 focus）
     */
    function findModelInstance(obj, depth = 0, visited = new Set()) {
        if (depth > 8 || !obj || typeof obj !== 'object' || visited.has(obj)) return null;
        visited.add(obj);

        // 放宽条件：只要有 internalModel 且 internalModel 有 coreModel
        if (obj.internalModel && obj.internalModel.coreModel) {
            return obj;
        }

        try {
            const keys = Object.keys(obj);
            for (const key of keys) {
                if (key.startsWith('_') && depth > 3) continue;
                const val = obj[key];
                if (!val || typeof val !== 'object') continue;
                const found = findModelInstance(val, depth + 1, visited);
                if (found) return found;
            }
        } catch (e) { /* ignore */ }

        return null;
    }

    /**
     * 深度搜索找到 coreModel（有 setParameterValueById 方法的对象）
     */
    function deepFindCoreModel(obj, depth = 0, visited = new Set()) {
        if (depth > 8 || !obj || typeof obj !== 'object' || visited.has(obj)) return null;
        visited.add(obj);

        if (typeof obj.setParameterValueById === 'function' && typeof obj.getParameterValueById === 'function') {
            return obj;
        }

        if (obj.internalModel?.coreModel?.setParameterValueById) {
            return obj.internalModel.coreModel;
        }

        try {
            const keys = Object.keys(obj);
            for (const key of keys) {
                if (key.startsWith('_') && depth > 3) continue;
                const val = obj[key];
                if (!val || typeof val !== 'object') continue;
                const found = deepFindCoreModel(val, depth + 1, visited);
                if (found) return found;
            }
        } catch (e) { /* ignore */ }

        return null;
    }

    /**
     * 彻底清除 focusController 内部残留状态
     */
    function killFocusController(internalModel) {
        const fc = internalModel.focusController;
        if (!fc) return;
        // 清除残留坐标（这些是看向右上角的元凶）
        if ('targetX' in fc) fc.targetX = 0;
        if ('targetY' in fc) fc.targetY = 0;
        if ('x' in fc) fc.x = 0;
        if ('y' in fc) fc.y = 0;
        if ('vx' in fc) fc.vx = 0;
        if ('vy' in fc) fc.vy = 0;
        // 禁用所有方法
        fc.update = () => {};
        fc.focus = () => {};
        if (fc.targetOn !== undefined) fc.targetOn = false;
    }

    /**
     * Hook 模型的 update 管线
     * 关键：在 internalModel.update 完成后（motions 已应用），覆写我们的参数
     */
    function hookModelUpdate() {
        if (hooked || !modelInstance) return false;

        const internalModel = modelInstance.internalModel;
        if (!internalModel) return false;

        // 1. 禁用鼠标追踪
        if (typeof modelInstance.focus === 'function') modelInstance.focus = () => {};
        if (typeof modelInstance.tap === 'function') modelInstance.tap = () => {};
        if ('autoInteract' in modelInstance) modelInstance.autoInteract = false;
        if ('autoFocus' in modelInstance) modelInstance.autoFocus = false;
        if ('autoHitTest' in modelInstance) modelInstance.autoHitTest = false;

        // 彻底清除 FocusController 的残留状态和方法
        killFocusController(internalModel);

        // 遍历 modelInstance 所有属性找 focus 相关的对象并禁用
        try {
            for (const key of Object.keys(modelInstance)) {
                const val = modelInstance[key];
                if (val && typeof val === 'object' && typeof val.focus === 'function' && key !== 'internalModel') {
                    val.focus = () => {};
                }
            }
        } catch(e) {}

        // 2. 停止自动 idle 动作播放
        if (internalModel.motionManager) {
            const mm = internalModel.motionManager;
            mm.startRandomMotion = () => Promise.resolve(false);
            if (mm.stopAllMotions) mm.stopAllMotions();
            else if (mm.reset) mm.reset();
            if (mm.groups) mm.groups.idle = '';
            if (mm.settings) mm.settings.idleMotionGroup = '';
            // 完全死掉 motionManager 的 update
            mm.update = () => false;
            console.log('[Live2D] 已停止 motionManager');
        }

        // 3. Hook internalModel.update — 在库更新后覆写参数
        const originalUpdate = internalModel.update.bind(internalModel);
        internalModel.update = (dt, now_timestamp) => {
            // 每帧强制重置 focusController（防止库内部重建）
            killFocusController(internalModel);
            originalUpdate(dt, now_timestamp);
            applyAllBehaviors();
        };

        hooked = true;
        console.log('[Live2D] ✅ 已 hook internalModel.update，参数覆写生效');
        return true;
    }

    /**
     * 所有行为参数的综合应用（在库 update 之后调用）
     */
    function applyAllBehaviors() {
        if (!coreModel || !modelReady) return;

        const currentTime = now();
        const deltaTime = lastTime > 0 ? (currentTime - lastTime) / 1000 : 0.016;
        lastTime = currentTime;

        const speakingMultiplier = isSpeaking ? 2.5 : 1.0;

        // 1. 眨眼
        applyBlink(currentTime);
        // 2. 身体/头部晃动
        applySway(deltaTime, speakingMultiplier);
        // 3. 呼吸
        applyBreath(deltaTime);
        // 4. 视线
        applyGaze(currentTime);
        // 5. 嘴巴
        applyMouth();
        // 6. 表情
        applyEmotion();
    }

    // ── 参数设置（直接调用 coreModel）───────────────────
    function setParam(paramId, value) {
        if (!coreModel) return;
        try {
            coreModel.setParameterValueById(paramId, value);
        } catch (e) { /* 参数可能不存在 */ }
    }

    // ── 眨眼 ────────────────────────────────────────────
    function scheduleNextBlink() {
        blink.nextBlinkTime = now() + randomRange(3000, 7000);
        blink.doubleBlinkPending = Math.random() < 0.2;
    }

    function applyBlink(currentTime) {
        if (!blink.blinking && currentTime >= blink.nextBlinkTime) {
            blink.blinking = true;
            blink.blinkStart = currentTime;
        }

        if (blink.blinking) {
            const elapsed = currentTime - blink.blinkStart;
            const duration = blink.blinkDuration;
            let eyeOpen;

            if (elapsed < duration / 2) {
                eyeOpen = 1.0 - (elapsed / (duration / 2));
            } else if (elapsed < duration) {
                eyeOpen = (elapsed - duration / 2) / (duration / 2);
            } else {
                eyeOpen = 1.0;
                blink.blinking = false;
                if (blink.doubleBlinkPending) {
                    blink.doubleBlinkPending = false;
                    blink.blinking = true;
                    blink.blinkStart = currentTime + 80;
                } else {
                    scheduleNextBlink();
                }
            }

            eyeOpen = Math.max(0, Math.min(1, eyeOpen));
            setParam('ParamEyeLOpen', eyeOpen);
            setParam('ParamEyeROpen', eyeOpen);
        } else {
            setParam('ParamEyeLOpen', 1.0);
            setParam('ParamEyeROpen', 1.0);
        }
    }

    // ── 身体/头部晃动 ───────────────────────────────────
    function applySway(deltaTime, multiplier) {
        // 说话时速度也加快 1.5x
        const speedMult = isSpeaking ? 1.5 : 1.0;

        sway.angleX.phase += sway.angleX.speed * deltaTime * speedMult;
        sway.angleY.phase += sway.angleY.speed * deltaTime * speedMult;
        sway.angleZ.phase += sway.angleZ.speed * deltaTime * speedMult;
        sway.bodyAngleX.phase += sway.bodyAngleX.speed * deltaTime * speedMult;
        sway.bodyAngleY.phase += sway.bodyAngleY.speed * deltaTime * speedMult;
        sway.bodyAngleZ.phase += sway.bodyAngleZ.speed * deltaTime * speedMult;

        let headX = Math.sin(sway.angleX.phase) * sway.angleX.amplitude * multiplier;
        let headY = Math.sin(sway.angleY.phase) * sway.angleY.amplitude * multiplier;
        let headZ = Math.sin(sway.angleZ.phase) * sway.angleZ.amplitude * multiplier;
        const bodyX = Math.sin(sway.bodyAngleX.phase) * sway.bodyAngleX.amplitude * multiplier;
        const bodyY = Math.sin(sway.bodyAngleY.phase) * sway.bodyAngleY.amplitude * (isSpeaking ? 2.0 : 0.5);
        const bodyZ = Math.sin(sway.bodyAngleZ.phase) * sway.bodyAngleZ.amplitude * multiplier;

        // 说话时添加额外手势动作
        if (isSpeaking) {
            const gestureValues = applySpeakGestures(deltaTime);
            headY += gestureValues.nod;       // 点头叠加
            headZ += gestureValues.tilt;      // 侧倾叠加
        }

        setParam('ParamAngleX', headX);
        setParam('ParamAngleY', headY);
        setParam('ParamAngleZ', headZ);
        setParam('ParamBodyAngleX', bodyX);
        setParam('ParamBodyAngleY', bodyY);
        setParam('ParamBodyAngleZ', bodyZ);
    }

    // ── 说话时的手势动作 ─────────────────────────────────
    function applySpeakGestures(deltaTime) {
        const currentTime = now();
        let nod = 0;
        let tilt = 0;

        // 随机点头（每 1~3 秒触发一次，持续 0.3~0.6 秒）
        if (!speakGesture.nodActive && currentTime >= speakGesture.nextNodTime) {
            speakGesture.nodActive = true;
            speakGesture.nodPhase = 0;
            speakGesture.nodEnd = currentTime + randomRange(300, 600);
            speakGesture.nodSpeed = (2 * Math.PI) / randomRange(0.3, 0.5);
        }
        if (speakGesture.nodActive) {
            speakGesture.nodPhase += speakGesture.nodSpeed * deltaTime;
            nod = Math.sin(speakGesture.nodPhase) * randomRange(3, 6);
            if (currentTime >= speakGesture.nodEnd) {
                speakGesture.nodActive = false;
                speakGesture.nextNodTime = currentTime + randomRange(1000, 3000);
            }
        }

        // 随机侧倾强调（每 2~5 秒）
        if (currentTime >= speakGesture.nextTiltTime) {
            speakGesture.tiltTarget = randomRange(-4, 4);
            speakGesture.nextTiltTime = currentTime + randomRange(2000, 5000);
        }
        speakGesture.tiltCurrent = lerp(speakGesture.tiltCurrent, speakGesture.tiltTarget, 0.02);
        tilt = speakGesture.tiltCurrent;

        // 偶尔挑眉（每 3~8 秒）
        if (currentTime >= speakGesture.nextBrowTime) {
            speakGesture.browTarget = Math.random() < 0.4 ? randomRange(0.3, 0.8) : 0;
            speakGesture.nextBrowTime = currentTime + randomRange(3000, 8000);
        }
        speakGesture.browRaise = lerp(speakGesture.browRaise, speakGesture.browTarget, 0.05);
        setParam('ParamBrowLY', speakGesture.browRaise);
        setParam('ParamBrowRY', speakGesture.browRaise);

        return { nod, tilt };
    }

    // ── 呼吸 ────────────────────────────────────────────
    function applyBreath(deltaTime) {
        breath.phase += breath.speed * deltaTime;
        const breathValue = (Math.sin(breath.phase) + 1) * 0.25;
        setParam('ParamBreath', breathValue);
    }

    // ── 视线 ────────────────────────────────────────────
    function scheduleNextGaze() {
        // 说话时视线转移更频繁（2~5秒 vs 5~15秒）
        if (isSpeaking) {
            gaze.nextGazeTime = now() + randomRange(2000, 5000);
        } else {
            gaze.nextGazeTime = now() + randomRange(5000, 15000);
        }
    }

    function applyGaze(currentTime) {
        if (!gaze.isGazing && currentTime >= gaze.nextGazeTime) {
            gaze.isGazing = true;
            // 说话时视线偏移范围稍大
            const range = isSpeaking ? 0.4 : 0.3;
            gaze.targetX = randomRange(-range, range);
            gaze.targetY = randomRange(-range * 0.5, range * 0.5);
            gaze.gazeEndTime = currentTime + randomRange(800, 2000);
        }

        if (gaze.isGazing && currentTime >= gaze.gazeEndTime) {
            gaze.isGazing = false;
            gaze.targetX = 0;
            gaze.targetY = 0;
            scheduleNextGaze();
        }

        gaze.currentX = lerp(gaze.currentX, gaze.targetX, gaze.lerpSpeed);
        gaze.currentY = lerp(gaze.currentY, gaze.targetY, gaze.lerpSpeed);

        setParam('ParamEyeBallX', gaze.currentX);
        setParam('ParamEyeBallY', gaze.currentY);
    }

    // ── 嘴巴 ────────────────────────────────────────────
    function applyMouth() {
        if (isSpeaking) {
            setParam('ParamMouthOpenY', mouthOpenValue);
        } else {
            setParam('ParamMouthOpenY', 0);
        }
    }

    // ── 表情叠加 ────────────────────────────────────────
    function applyEmotion() {
        for (const [paramId, value] of Object.entries(emotionParams)) {
            setParam(paramId, value);
        }
    }

    // ══════════════════════════════════════════════════
    // 模型获取与初始化
    // ══════════════════════════════════════════════════

    /**
     * 轮询等待模型就绪，找到后 hook update
     */
    function waitForModel() {
        let attempts = 0;
        const maxAttempts = 120; // 最多等 60 秒

        const pollInterval = setInterval(() => {
            attempts++;

            if (!oml2d) return;

            // 先找 pixi-live2d-display 模型实例
            if (!modelInstance) {
                modelInstance = findModelInstance(oml2d);
            }

            // 再找 coreModel
            if (!coreModel) {
                coreModel = deepFindCoreModel(oml2d);
            }

            if (coreModel && modelInstance) {
                clearInterval(pollInterval);
                modelReady = true;
                console.log('[Live2D] ✅ coreModel + modelInstance 均获取成功');
                setupFloatingPet();

                // 初始化行为参数
                initSwayParams();
                scheduleNextBlink();
                scheduleNextGaze();
                lastTime = now();

                // Hook update 管线（关键！）
                const hookSuccess = hookModelUpdate();
                if (!hookSuccess) {
                    console.warn('[Live2D] hook 失败，退回 RAF 方式（可能被覆盖）');
                    startFallbackRAF();
                }
                return;
            }

            if (coreModel && !modelInstance) {
                // 有 coreModel 但没有 modelInstance，尝试只 hook coreModel
                if (attempts >= 40) {
                    clearInterval(pollInterval);
                    modelReady = true;
                    console.log('[Live2D] 找到 coreModel 但未找到 modelInstance，使用 ticker hook');
                    initSwayParams();
                    scheduleNextBlink();
                    scheduleNextGaze();
                    lastTime = now();
                    hookViaPixiTicker();
                    return;
                }
            }

            if (attempts >= maxAttempts) {
                clearInterval(pollInterval);
                console.warn('[Live2D] 最终未能获取模型实例，自然行为不可用');
            }
        }, 500);
    }

    function setupFloatingPet() {
        if (!modelInstance) return;
        const appWrapper = document.getElementById('app-wrapper');
        if (appWrapper) {
            appWrapper.style.setProperty('width', '100vw', 'important');
            appWrapper.style.setProperty('max-width', '100vw', 'important');
        }
        const liveCanvas = oml2d?.pixiApp?.view || oml2d?.pixiApp?.renderer?.view;
        if (liveCanvas) {
            liveCanvas.style.position = 'fixed';
            liveCanvas.style.inset = '0';
            liveCanvas.style.width = '100vw';
            liveCanvas.style.height = '100vh';
            liveCanvas.style.zIndex = '2';
            liveCanvas.style.pointerEvents = 'none';
        }
        const storageKey = 'fufu-live2d-layout-v1';
        try { petLayout = JSON.parse(localStorage.getItem(storageKey) || 'null'); } catch (_) { petLayout = null; }

        const initialBounds = modelInstance.getBounds?.();
        if (!petLayout && initialBounds?.height > 0 && modelInstance.scale) {
            const baseScale = modelInstance.scale.x || 0.15;
            const targetHeight = Math.min(window.innerHeight * 0.42, 390);
            const factor = targetHeight / initialBounds.height;
            modelInstance.scale.set(baseScale * factor);
            petLayout = { x: 0.84, y: 0.72 };
        }
        if (!petLayout) petLayout = { x: 0.84, y: 0.72 };
        else if (Number.isFinite(petLayout.scale) && modelInstance.scale?.set) {
            modelInstance.scale.set(Math.max(0.035, Math.min(0.32, petLayout.scale)));
        }

        const movePetCenter = (x, y) => {
            const bounds = modelInstance.getBounds?.();
            if (!bounds) {
                modelInstance.x = x;
                modelInstance.y = y;
                return;
            }
            modelInstance.x += x - (bounds.x + bounds.width / 2);
            modelInstance.y += y - (bounds.y + bounds.height / 2);
        };
        if (!Number.isFinite(petLayout.x)) petLayout.x = 0.84;
        if (!Number.isFinite(petLayout.y)) petLayout.y = 0.72;
        movePetCenter(window.innerWidth * petLayout.x, window.innerHeight * petLayout.y);

        dragHandle = document.getElementById('live2d-pet-handle');
        if (!dragHandle) {
            dragHandle = document.createElement('div');
            dragHandle.id = 'live2d-pet-handle';
            dragHandle.setAttribute('role', 'group');
            dragHandle.setAttribute('aria-label', '可拖动的芙芙桌宠；滚轮或加减按钮调整大小');
            dragHandle.innerHTML = '<div class="live2d-pet-size"><button type="button" data-size="smaller" aria-label="缩小桌宠" title="缩小">−</button><button type="button" data-size="larger" aria-label="放大桌宠" title="放大">+</button></div>';
            document.body.appendChild(dragHandle);
        }

        const persist = () => {
            const b = modelInstance.getBounds?.();
            petLayout.x = Math.max(0, Math.min(1, (b.x + b.width / 2) / window.innerWidth));
            petLayout.y = Math.max(0, Math.min(1, (b.y + b.height / 2) / window.innerHeight));
            petLayout.scale = modelInstance.scale?.x || 0.15;
            try { localStorage.setItem(storageKey, JSON.stringify(petLayout)); } catch (_) {}
        };
        const placeHandle = () => {
            const b = modelInstance.getBounds?.();
            if (!b || !Number.isFinite(b.x) || !Number.isFinite(b.width)) return;
            dragHandle.style.left = `${b.x}px`;
            dragHandle.style.top = `${b.y}px`;
            dragHandle.style.width = `${Math.max(50, b.width)}px`;
            dragHandle.style.height = `${Math.max(80, b.height)}px`;
            dragHandle.classList.add('ready');
            window.dispatchEvent(new CustomEvent('live2d:layout', {
                detail: { x: b.x, y: b.y, width: b.width, height: b.height },
            }));
        };

        let pointer = null;
        dragHandle.addEventListener('pointerdown', (event) => {
            if (event.target.closest('button')) return;
            pointer = { id: event.pointerId, x: event.clientX, y: event.clientY };
            dragHandle.classList.add('dragging');
            dragHandle.setPointerCapture(event.pointerId);
            event.preventDefault();
        });
        dragHandle.addEventListener('pointermove', (event) => {
            if (!pointer || pointer.id !== event.pointerId) return;
            modelInstance.x += event.clientX - pointer.x;
            modelInstance.y += event.clientY - pointer.y;
            pointer.x = event.clientX;
            pointer.y = event.clientY;
            placeHandle();
        });
        const endDrag = (event) => {
            if (!pointer || pointer.id !== event.pointerId) return;
            pointer = null;
            dragHandle.classList.remove('dragging');
            persist();
        };
        dragHandle.addEventListener('pointerup', endDrag);
        dragHandle.addEventListener('pointercancel', endDrag);

        const resizePet = (factor) => {
            const current = modelInstance.scale?.x || 0.15;
            const next = Math.max(0.035, Math.min(0.32, current * factor));
            modelInstance.scale.set(next);
            placeHandle();
            persist();
        };
        dragHandle.querySelectorAll('[data-size]').forEach((button) => {
            button.addEventListener('click', (event) => {
                event.stopPropagation();
                resizePet(button.dataset.size === 'larger' ? 1.12 : 1 / 1.12);
            });
        });
        dragHandle.addEventListener('wheel', (event) => {
            event.preventDefault();
            resizePet(event.deltaY < 0 ? 1.08 : 1 / 1.08);
        }, { passive: false });

        window.addEventListener('resize', () => {
            movePetCenter(
                Math.min(window.innerWidth - 30, Math.max(30, petLayout.x * window.innerWidth)),
                Math.min(window.innerHeight - 40, Math.max(40, petLayout.y * window.innerHeight)),
            );
            placeHandle();
            persist();
        });
        placeHandle();
    }

    /**
     * 备用方案：通过 PIXI ticker 注入我们的更新
     * 用最低优先级确保在模型 update 之后执行
     */
    function hookViaPixiTicker() {
        if (oml2d?.pixiApp?.ticker) {
            // PIXI ticker 优先级: 负值 = 后执行。用非常低的值确保最后
            oml2d.pixiApp.ticker.add(() => {
                applyAllBehaviors();
            }, null, -1000);
            console.log('[Live2D] ✅ 已通过 PIXI ticker (priority=-1000) 注入行为更新');

            // 同时尝试禁用鼠标追踪和动作
            disableLibraryBehaviors();
        } else {
            console.warn('[Live2D] pixiApp.ticker 不可用，退回 RAF');
            startFallbackRAF();
        }
    }

    /**
     * 禁用库的内置行为（鼠标追踪、自动动作）— 即使没有找到 modelInstance
     */
    function disableLibraryBehaviors() {
        if (!oml2d) return;

        // 通过 pixiApp.stage.children 遍历找到模型
        const stage = oml2d.pixiApp?.stage;
        if (stage?.children) {
            for (const child of stage.children) {
                if (child?.internalModel) {
                    // 禁用 focus
                    if (typeof child.focus === 'function') child.focus = () => {};
                    // 禁用 motionManager
                    if (child.internalModel.motionManager) {
                        child.internalModel.motionManager.startRandomMotion = () => Promise.resolve(false);
                        child.internalModel.motionManager.update = () => false;
                    }
                    // 禁用 focusController 并清除残留坐标
                    if (child.internalModel.focusController) {
                        const fc = child.internalModel.focusController;
                        fc.update = () => {};
                        fc.focus = () => {};
                        if ('targetX' in fc) fc.targetX = 0;
                        if ('targetY' in fc) fc.targetY = 0;
                        if ('x' in fc) fc.x = 0;
                        if ('y' in fc) fc.y = 0;
                    }
                    console.log('[Live2D] 通过 stage.children 禁用了库行为');
                }
            }
        }

        // 也尝试通过 oml2d.models 路径
        if (oml2d.models) {
            const models = oml2d.models;
            const keys = Object.keys(models);
            for (const key of keys) {
                const m = models[key];
                if (m?.internalModel) {
                    if (typeof m.focus === 'function') m.focus = () => {};
                    if (m.internalModel.motionManager) {
                        m.internalModel.motionManager.startRandomMotion = () => Promise.resolve(false);
                        m.internalModel.motionManager.update = () => false;
                    }
                    if (m.internalModel.focusController) {
                        const fc = m.internalModel.focusController;
                        fc.update = () => {};
                        fc.focus = () => {};
                        if ('targetX' in fc) fc.targetX = 0;
                        if ('targetY' in fc) fc.targetY = 0;
                        if ('x' in fc) fc.x = 0;
                        if ('y' in fc) fc.y = 0;
                    }
                }
            }
        }
    }

    /**
     * 最终退路：RAF 循环（可能被库覆盖，但总比没有好）
     */
    function startFallbackRAF() {
        function loop() {
            applyAllBehaviors();
            requestAnimationFrame(loop);
        }
        requestAnimationFrame(loop);
    }

    // ── 初始化 ────────────────────────────────────────
    async function init() {
        if (typeof OML2D === 'undefined' && typeof window.OML2D === 'undefined') {
            console.error('[Live2D] oh-my-live2d 未加载');
            return;
        }

        const OML2DLib = window.OML2D || OML2D;

        try {
            oml2d = OML2DLib.loadOml2d({
                primaryColor: 'transparent',
                sayHello: false,
                models: [
                    {
                        path: MODEL_PATH,
                        scale: 0.15,
                        position: [0, 0],
                        stageStyle: {
                            width: '100vw',
                            height: '100vh',
                        },
                        // 禁用模型的鼠标跟随
                        showHitAreaFrames: false,
                    },
                ],
                parentElement: document.body,
                stageStyle: {
                    position: 'fixed',
                    top: '0',
                    right: 'auto',
                    left: '0',
                    width: '100vw',
                    height: '100vh',
                    zIndex: '2',
                    pointerEvents: 'none',
                    background: 'transparent',
                },
                statusBar: { disable: true },
                menus: { disable: true },
                tips: { disable: true },
            });

            console.log('[Live2D] oh-my-live2d 初始化完成');
            console.log('[Live2D] oml2d keys:', Object.keys(oml2d));

            // 开始轮询等待模型就绪
            waitForModel();
        } catch (err) {
            console.error('[Live2D] 初始化失败:', err);
        }
    }

    // ── 公共 API ──────────────────────────────────────

    function setSpeaking(speaking) {
        isSpeaking = !!speaking;
        if (!isSpeaking) {
            mouthOpenValue = 0;
        }
        console.log('[Live2D] setSpeaking:', isSpeaking);
    }

    function setMouthOpen(value) {
        if (!isSpeaking) return;
        mouthOpenValue = Math.max(0, Math.min(1, value));
    }

    function setEmotion(params) {
        if (!params || typeof params !== 'object') {
            emotionParams = {};
            return;
        }
        emotionParams = { ...params };
    }

    function playMotion(group, index) {
        // 注意：由于我们停止了自动动作，手动播放仍可通过此接口触发
        if (!modelReady || !modelInstance) {
            console.log('[Live2D] playMotion: 模型未就绪');
            return;
        }
        try {
            if (modelInstance.motion) {
                modelInstance.motion(group, index);
            } else if (modelInstance.internalModel?.motionManager) {
                modelInstance.internalModel.motionManager.startMotionByGroup(group, index);
            }
            console.log('[Live2D] playMotion:', group, index);
        } catch (e) {
            console.warn('[Live2D] playMotion 失败:', e);
        }
    }

    function getModel() {
        return coreModel || oml2d;
    }

    function isReady() {
        return modelReady;
    }

    // ── 暴露 API ──────────────────────────────────────
    return {
        init,
        setSpeaking,
        setMouthOpen,
        setEmotion,
        playMotion,
        getModel,
        isReady,
        getBounds: () => {
            if (!modelInstance?.getBounds) return null;
            const b = modelInstance.getBounds();
            return { x: b.x, y: b.y, width: b.width, height: b.height };
        },
    };
})();

// ── 页面加载完成后自动初始化 ──
window.addEventListener('DOMContentLoaded', () => {
    setTimeout(() => {
        Live2DManager.init();
    }, 300);
});
