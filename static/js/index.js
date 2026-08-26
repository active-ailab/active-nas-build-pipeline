tailwind.config = {
  theme: {
    extend: {
      colors: { primary: '#165DFF', success: '#00B42A', wait: '#909399' }
    }
  }
};

let currentStep = 1;
const totalStep = 8;

// 用户隔离：每个浏览器生成唯一 ID，存 localStorage（跨 webview 重载持久化）
function getUserId() {
  let uid = localStorage.getItem('fw_user_id');
  if (!uid) {
    uid = 'u_' + Date.now().toString(36) + '_' + Math.random().toString(36).substr(2, 6);
    localStorage.setItem('fw_user_id', uid);
  }
  return uid;
}
const currentUserId = getUserId();

// ================== Jenkins 登录管理 ==================
function getStoredCredential() {
  try {
    const raw = localStorage.getItem('jenkins_cred');
    if (!raw) return null;
    return JSON.parse(atob(raw));
  } catch { return null; }
}

function setStoredCredential(user, token) {
  localStorage.setItem('jenkins_cred', btoa(JSON.stringify({ user, token })));
}

function clearStoredCredential() {
  localStorage.removeItem('jenkins_cred');
}

function getJenkinsAuth() {
  const cred = getStoredCredential();
  if (!cred || !cred.user || !cred.token) return null;
  return { username: cred.user, password: cred.token };
}

// 飞书通知：open_id 存 localStorage，随构建请求发送
function saveNotifyOpenId() {
  const val = document.getElementById('notifyOpenIdInput')?.value?.trim() || '';
  if (val && val.startsWith('ou_')) localStorage.setItem('fw_notify_open_id', val);
}
function getNotifyOpenId() {
  return localStorage.getItem('fw_notify_open_id') || '';
}
function initNotifyOpenIdInput() {
  const el = document.getElementById('notifyOpenIdInput');
  if (!el) return;
  const saved = getNotifyOpenId();
  if (saved) {
    el.value = saved;
    el.style.color = '#4ade80';  // green = bound
    el.title = '已绑定飞书通知';
    return;
  }
  // ── 自动检测：飞书容器打开应用时，URL 中可能携带 open_id 参数 ──
  const urlParams = new URLSearchParams(window.location.search);
  const urlOpenId = urlParams.get('open_id');
  if (urlOpenId && urlOpenId.startsWith('ou_')) {
    localStorage.setItem('fw_notify_open_id', urlOpenId);
    el.value = urlOpenId;
    el.style.color = '#4ade80';
    el.style.width = '140px';
    el.title = '已绑定飞书通知（自动检测）';
    return;
  }
  // 未绑定 + URL 无 open_id → 自动获取绑定码（浏览器直接打开时使用）
  el.placeholder = '绑定中...';
  el.style.cursor = 'default';
  autoBindFeishuNotify(el);
}

async function autoBindFeishuNotify(el) {
  try {
    // ── 优先尝试飞书容器免登（自动获取用户 open_id，无需手动操作）──
    el.placeholder = '免登检测中…';
    const autoOpenId = await tryAutoLoginFromFeishu(el);
    if (autoOpenId) {
      localStorage.setItem('fw_notify_open_id', autoOpenId);
      el.value = autoOpenId;
      el.style.color = '#4ade80';
      el.style.width = '140px';
      el.title = '已绑定飞书通知（自动免登）';
      return;
    }

    // ── 回退：URL 参数检测（飞书可能将 open_id 带入 URL）──
    const urlOpenId = getNotifyOpenId();
    if (urlOpenId) {
      el.value = urlOpenId;
      el.style.color = '#4ade80';
      el.style.width = '140px';
      el.title = '已绑定飞书通知';
      return;
    }

    // ── 最终回退：绑定码流程（免登失败 / 浏览器直开时使用）──
    const r1 = await fetch('/api/bind/start');
    const d1 = await r1.json();
    if (!d1.ok || !d1.bind_code) { el.placeholder = '获取失败'; return; }
    const code = d1.bind_code;
    el.value = code;
    el.style.color = '#fbbf24';
    // hint 里附带免登失败原因（如果有的话）
    el.title = _lastAutoLoginError || '请私聊飞书Bot发送此绑定码';
    el.style.width = '180px';

    let attempts = 0;
    const poll = setInterval(async () => {
      attempts++;
      if (attempts > 75) { clearInterval(poll); el.placeholder = '绑定超时'; return; }
      try {
        const r2 = await fetch('/api/bind/check?code=' + encodeURIComponent(code));
        const d2 = await r2.json();
        if (d2.ok && d2.open_id) {
          clearInterval(poll);
          localStorage.setItem('fw_notify_open_id', d2.open_id);
          el.value = d2.open_id;
          el.style.color = '#4ade80';
          el.style.width = '140px';
          el.title = '✅ 已绑定飞书通知';
          el.setAttribute('readonly', '');
        }
      } catch(e) {}
    }, 2000);
  } catch(e) {
    el.placeholder = '网络错误';
  }
}

// ── 飞书免登：通过 JSSDK 自动获取用户 open_id ──
let _feishuAutoLoginAttempted = false;
let _lastAutoLoginError = '';

async function tryAutoLoginFromFeishu(el) {
  if (_feishuAutoLoginAttempted) return null;
  _feishuAutoLoginAttempted = true;

  // ── 等待 JSSDK ──
  var waitTime = 0;
  while (waitTime < 10000) {
    if (window.h5sdk || window.tt) break;
    await new Promise(function(r) { setTimeout(r, 500); });
    waitTime += 500;
  }
  if (!window.h5sdk && !window.tt) {
    _lastAutoLoginError = 'JSSDK未加载';
    return null;
  }

  // ── 等待 h5sdk ready ──
  try {
    if (window.h5sdk && window.h5sdk.ready) {
      await new Promise(function(resolve) { window.h5sdk.ready(resolve); setTimeout(resolve, 5000); });
    }
  } catch(e) {}

  if (!window.tt) {
    _lastAutoLoginError = 'window.tt 不存在';
    return null;
  }

  if (typeof tt.requestAuthCode !== 'function') {
    _lastAutoLoginError = 'API不存在: requestAuthCode';
    return null;
  }

  try {
    console.log('[FeishuAuto] 调用 requestAuthCode...');

    // ── 步骤 1: 获取授权码 ──
    // 不同 JSSDK 版本参数名有差异：appId / appID / app_id
    var APP_ID = 'cli_a92812f1c1f9dbb5';
    var authCode = null;

    // 尝试 1: appId (小写 d)
    try {
      authCode = await new Promise(function(resolve, reject) {
        tt.requestAuthCode({
          appId: APP_ID,
          success: function(res) { resolve(res.code || ''); },
          fail: function(err) { reject(new Error(JSON.stringify(err))); },
        });
      });
    } catch(e) {
      console.log('[FeishuAuto] appId 参数失败，尝试其他格式...');
    }

    // 尝试 2: appID (大写 D)
    if (!authCode) {
      try {
        authCode = await new Promise(function(resolve, reject) {
          tt.requestAuthCode({
            appID: APP_ID,
            success: function(res) { resolve(res.code || ''); },
            fail: function(err) { reject(new Error(JSON.stringify(err))); },
          });
        });
      } catch(e) {
        console.log('[FeishuAuto] appID 参数失败，尝试 getUserInfo...');
      }
    }

    // 尝试 3: 无参数直接调用
    if (!authCode && typeof tt.getUserInfo === 'function') {
      try {
        var info = await new Promise(function(resolve, reject) {
          tt.getUserInfo({
            success: function(res) { resolve(res); },
            fail: function(err) { reject(new Error(JSON.stringify(err))); },
          });
        });
        if (info && info.open_id && info.open_id.startsWith('ou_')) {
          _lastAutoLoginError = '';
          console.log('[FeishuAuto] ✅ getUserInfo 返回 open_id');
          return info.open_id;
        }
        console.log('[FeishuAuto] getUserInfo 无 open_id, result:', JSON.stringify(info));
      } catch(e) {
        console.log('[FeishuAuto] getUserInfo 失败');
      }
    }

    if (!authCode) {
      _lastAutoLoginError = 'requestAuthCode/getUserInfo均失败';
      return null;
    }

    if (!authCode) {
      _lastAutoLoginError = '未获取到授权码';
      return null;
    }

    // ── 步骤 2: 后端换取 open_id（用 XHR 避免 fetch 兼容性问题）──
    console.log('[FeishuAuto] 向后端换取 open_id...');

    var openId = await new Promise(function(resolve, reject) {
      var xhr = new XMLHttpRequest();
      xhr.open('POST', '/api/feishu/auto-bind', true);
      xhr.setRequestHeader('Content-Type', 'application/json');
      xhr.timeout = 15000;
      xhr.onload = function() {
        if (xhr.status === 200) {
          try {
            var data = JSON.parse(xhr.responseText);
            if (data.ok && data.open_id) {
              console.log('[FeishuAuto] ✅ 免登成功: ' + data.open_id);
              resolve(data.open_id);
            } else {
              reject(new Error(data.error || '后端返回异常'));
            }
          } catch(parseErr) {
            reject(new Error('解析响应失败'));
          }
        } else {
          reject(new Error('HTTP ' + xhr.status));
        }
      };
      xhr.onerror = function() {
        reject(new Error('网络请求失败(XHR)'));
      };
      xhr.ontimeout = function() {
        reject(new Error('请求超时'));
      };
      xhr.send(JSON.stringify({ code: authCode }));
    });

    _lastAutoLoginError = '';
    return openId;

  } catch(e) {
    _lastAutoLoginError = '免登失败: ' + (e.message || String(e));
    console.error('[FeishuAuto] ' + _lastAutoLoginError);
    return null;
  }
}

function updateLoginUI() {
  const cred = getStoredCredential();
  const statusEl = document.getElementById('loginUsername');
  const loginBtn = document.getElementById('loginBtn');
  const logoutBtn = document.getElementById('logoutBtn');
  const statsBtn = document.getElementById('statsBtn');
  if (cred && cred.user) {
    if (statusEl) statusEl.textContent = cred.user;
    if (loginBtn) loginBtn.classList.add('hidden');
    if (logoutBtn) logoutBtn.classList.remove('hidden');
    // 仅 cs-guoqifa 显示统计按钮
    if (statsBtn) {
      if (cred.user.toLowerCase() === 'cs-guoqifa') {
        statsBtn.classList.remove('hidden');
      } else {
        statsBtn.classList.add('hidden');
      }
    }
  } else {
    if (statusEl) statusEl.textContent = '未登录';
    if (loginBtn) loginBtn.classList.remove('hidden');
    if (logoutBtn) logoutBtn.classList.add('hidden');
    if (statsBtn) statsBtn.classList.add('hidden');
    if (loginBtn) loginBtn.classList.remove('hidden');
    if (logoutBtn) logoutBtn.classList.add('hidden');
  }
}

function showLoginModal() {
  document.getElementById('loginOverlay').classList.remove('hidden');
  document.getElementById('loginCancelBtn').classList.remove('hidden');
  document.getElementById('loginUserInput').value = '';
  document.getElementById('loginTokenInput').value = '';
  document.getElementById('loginError').classList.add('hidden');
  const cred = getStoredCredential();
  if (cred && cred.user) document.getElementById('loginUserInput').value = cred.user;
}

function hideLoginModal() {
  document.getElementById('loginOverlay').classList.add('hidden');
}

// 强制登录模式：未登录时弹出登录框（由 nextStep1 等触发）
function forceLogin() {
  document.getElementById('loginOverlay').classList.remove('hidden');
  document.getElementById('loginCancelBtn').classList.add('hidden');
}

// 显示/隐藏卡通引导横幅
function showLoginBanner() {
  const banner = document.getElementById('loginGuideBanner');
  if (banner) banner.classList.remove('hidden');
}
function hideLoginBanner() {
  const banner = document.getElementById('loginGuideBanner');
  if (banner) banner.classList.add('hidden');
}

function releaseLogin() {
  document.getElementById('loginOverlay').classList.add('hidden');
  document.getElementById('loginCancelBtn').classList.remove('hidden');
  hideLoginBanner();
}

async function doLogin() {
  const user = document.getElementById('loginUserInput').value.trim();
  const token = document.getElementById('loginTokenInput').value.trim();
  const errEl = document.getElementById('loginError');
  if (!user || !token) {
    errEl.textContent = '请输入账号和密码/Token';
    errEl.classList.remove('hidden');
    return;
  }
  errEl.classList.add('hidden');
  try {
    const resp = await fetch('/api/auth/verify', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: user, token: token }),
    });
    const data = await resp.json();
    if (data.ok) {
      setStoredCredential(user, token);
      updateLoginUI();
      hideLoginModal();
      releaseLogin();
    } else {
      errEl.textContent = '❌ ' + (data.error || '验证失败');
      errEl.classList.remove('hidden');
    }
  } catch (err) {
    errEl.textContent = '❌ 网络错误: ' + err.message;
    errEl.classList.remove('hidden');
  }
}

function doLogout() {
  if (confirm('确定退出登录吗？')) {
    clearStoredCredential();
    updateLoginUI();
    showLoginBanner();
  }
}

// 页面加载时验证已保存的凭据是否仍然有效
async function verifyStoredCredential() {
  initNotifyOpenIdInput();  // 恢复已保存的飞书通知 open_id
  const cred = getStoredCredential();
  if (!cred || !cred.user || !cred.token) {
    updateLoginUI();
    showLoginBanner();  // 未登录显示卡通引导横幅
    return;
  }
  try {
    const resp = await fetch('/api/auth/verify', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: cred.user, token: cred.token }),
    });
    const data = await resp.json();
    if (data.ok) {
      updateLoginUI();
      hideLoginBanner();
    } else {
      clearStoredCredential();
      updateLoginUI();
      showLoginBanner();
    }
  } catch {
    // 网络错误：允许继续（可能 Jenkins 暂时不可达）
    updateLoginUI();
    hideLoginBanner();
  }
}
let selectedVersionData = null;
let publishType = 'oldProject';
let skipJenkinsMode = false;  // 版本编译完成直接生成文档模式
let isTscanStandalone = false; // 独立 TSCAN 构建模式
let selectedPlatform = '';
let selectedDevice = '';
let canGoNextStep4 = false;
const buildContentOptions = [
  { value: 'OTA', label: 'OTA', defaultChecked: true },
  { value: 'OTA_SLEEP', label: 'OTA_SLEEP', defaultChecked: true },
  { value: 'FCT', label: 'FCT', defaultChecked: true },
  { value: 'BOOT', label: 'BOOT', defaultChecked: true },
  { value: 'RECOVERY', label: 'RECOVERY', defaultChecked: true },
  { value: 'HWTEST', label: 'HWTEST', defaultChecked: false }
];
const versionThreePartRegex = /^\d+\.\d+\.\d+$/; 

const publishTypeMap = { oldProject: "基于历史版本参数发版", newProject: "重新填写新项目参数发版", skipJenkins: "版本编译完成直接生成文档", changelog: "生成版本差分 changelog", tscan: "基于历史版本构建 TSCAN" };
const versionRegex = /^\d+\.\d+\.\d+\.\d+$/;

// ================== 第八步 编译进度 全局变量 ==================
let compileSteps = [
  { name: "版本编译", status: "waiting", percent: 0 },
  { name: "本地下载", status: "waiting", percent: 0 },
  { name: "NAS文件上传", status: "waiting", percent: 0 },
  { name: "分享链接", status: "waiting", percent: 0 },
  { name: "生成飞书文档", status: "waiting", percent: 0 }
];
let totalPercent = 0;
let logList = [];
// [2026-06-02] 注释：btnBackFrom8 已删除，prevStepBefore8 无消费者；测试无影响后移除
// let prevStepBefore8 = 7;  // 记录从哪个步骤跳到第8步，用于失败后"返回上一步"
window._jenkinsJobUrl = '';  // 跳转到 Jenkins 的 job URL（静态，基于平台选择）
window._taskPlatform = '';    // 当前任务的平台（mhs003 / mhs003s）
window._taskType = '';        // 当前任务类型

function setBuildParamPlaceholder() {
  const placeholders = {
    tag_algo: "发版分支最新tag", tag_boot: "xxx-bootloader-1.0.0",
    tag_recovery: "xxx-recovery-1.0.0", tag_fct: "xxx-fct-1.0.0",
    ver_release: "1.3.0", ver_debug: "1.3.1", ver_fct: "1.3.0",
    diff_name: "1.2.0.1", diff_code: "202605181540"
  };
  Object.keys(placeholders).forEach(id => {
    const el = document.getElementById(id);
    if (el) el.placeholder = placeholders[id];
  });
}

document.addEventListener('DOMContentLoaded', function() {
  // 全局禁用自动填充（1~7 步所有输入框）
  document.querySelectorAll('input, select, textarea').forEach(el => {
    el.setAttribute('autocomplete', 'off');
  });

  initBuildContentCheckboxes('');
  setBuildParamPlaceholder();
  initDragRunningTaskBtn();  // 初始化浮动任务按钮拖拽+点击
  checkRunningTasksOnLoad();
  verifyStoredCredential();

  // 初始化 Wiki Token 可搜索下拉框（Tom Select）
  initWikiTokenSelect();
  // 初始化发版分支可搜索下拉框（Tom Select）
  initBranchSelect();
});

function toggleForm(f, t) {
  const formFrom = document.getElementById(`form${f}`);
  const formTo = document.getElementById(`form${t}`);
  if (formFrom) formFrom.style.display = 'none';
  if (formTo) formTo.style.display = 'block';
}

function updateStepStatus(c, t) {
  const step4Text = document.querySelector('#step4')?.nextElementSibling;
  const form4Title = document.querySelector('#form4 .step-heading');
  if(publishType === 'newProject' && step4Text && form4Title){
    step4Text.textContent = '新项目参数发版'; form4Title.textContent = '4. 新项目参数发版';
  }else if(step4Text && form4Title){
    step4Text.textContent = '选择历史版本'; form4Title.textContent = '4. 选择历史版本';
  }
  for(let i=1;i<=totalStep;i++){
    const s = document.getElementById(`step${i}`); if(!s) continue;
    const label = s.nextElementSibling;
    const node = s.parentElement;
    const l = (i <= 7) ? document.getElementById(`line${i}`) : null;
    if(i < t){
      // 已完成
      s.classList.remove('bg-wait'); s.classList.add('bg-success','text-white');
      if(l){ l.classList.remove('bg-gray-200'); l.classList.add('bg-success'); }
      if(label){ label.className = 'step-label success'; }
      if(node){ node.classList.remove('current'); }
    } else if(i === t){
      // 当前步骤
      s.classList.remove('bg-wait'); s.classList.add('bg-primary','text-white');
      if(label){ label.className = 'step-label active'; }
      if(node){ node.classList.add('current'); }
    } else {
      // 未激活
      s.classList.remove('bg-primary','bg-success'); s.classList.add('bg-wait','text-white');
      if(l){ l.classList.remove('bg-success'); l.classList.add('bg-gray-200'); }
      if(label){ label.className = 'step-label waiting'; }
      if(node){ node.classList.remove('current'); }
    }
  }
}

function toggleFwVerStrategyVisibility() {
  const isMhs = selectedPlatform === 'MHS003/MHS003S';
  const fwVerWrap = document.getElementById('fwVerStrategyWrap');
  const final7Wrap = document.getElementById('final7FwVerStrategyWrap');
  if (fwVerWrap) { fwVerWrap.classList.toggle('hidden', !isMhs); }
  if (final7Wrap) { final7Wrap.classList.toggle('hidden', !isMhs); }
}

function nextStep1() {
  // 未登录时弹出登录框，阻止下一步
  if (!getJenkinsAuth()) {
    forceLogin();
    return;
  }
  const p = document.querySelector('input[name="platform"]:checked'); if(!p){ alert('请选择设备平台'); return; }
  selectedPlatform = p.value; toggleFwVerStrategyVisibility();
  const currentPlatformEl = document.getElementById('currentPlatform');
  if (currentPlatformEl) currentPlatformEl.textContent = selectedPlatform;
  const success = fillDeviceList(selectedPlatform);
  if (!success) { return; }
  toggleForm(1,2); updateStepStatus(1,2); currentStep = 2;
}

function nextStep2() {
  const deviceSelectEl = document.getElementById('deviceSelect'); if(!deviceSelectEl){ alert('目标项目选择框未找到'); return; }
  const d = deviceSelectEl.value; if(!d){ alert('请选择目标项目'); return; }
  selectedDevice = d;
  selectWikiTokenForDevice(d);  // 自动匹配当前设备的 Wiki Token
  loadBranchOptions(d, true);  // 加载发版分支下拉选项，切换设备时清空旧值
  const confirmPlatformEl = document.getElementById('confirmPlatform'); const confirmDeviceEl = document.getElementById('confirmDevice');
  if (confirmPlatformEl) confirmPlatformEl.textContent = selectedPlatform;
  if (confirmDeviceEl) confirmDeviceEl.textContent = selectedDevice;
  toggleForm(2,3); updateStepStatus(2,3); currentStep = 3;
}

function nextStep3() {
  const t = document.querySelector('input[name="publishType"]:checked'); if(!t){ alert('请选择功能入口'); return; }
  publishType = t.value;

  if (publishType === 'changelog') {
    // 弹框填写 changelog 参数
    openChangelogModal();
    return;
  }

  if (publishType === 'skipJenkins') {
    // 版本编译完成直接生成文档模式
    skipJenkinsMode = true;
    setInputPlaceholder(true);
    const paramStepPlatformEl = document.getElementById('paramStepPlatform');
    const paramStepDeviceEl = document.getElementById('paramStepDevice');
    const paramStepPublishTypeEl = document.getElementById('paramStepPublishType');
    const paramStepVersionWrapEl = document.getElementById('paramStepVersionWrap');
    if (paramStepPlatformEl) paramStepPlatformEl.textContent = selectedPlatform;
    if (paramStepDeviceEl) paramStepDeviceEl.textContent = selectedDevice;
    if (paramStepPublishTypeEl) paramStepPublishTypeEl.textContent = '版本编译完成直接生成文档';
    if (paramStepVersionWrapEl) paramStepVersionWrapEl.classList.add('hidden');
    const paramStepHistDetailEl = document.getElementById('paramStepHistDetailWrap');
    if (paramStepHistDetailEl) paramStepHistDetailEl.classList.add('hidden');
    clearAllInput();
    const projectIdEl = document.getElementById('projectId');
    if (projectIdEl) projectIdEl.value = selectedDevice || '';
    setBuildParamPlaceholder();

    // 显示 Jenkins URL 区域，隐藏普通按钮
    const jenkinsSection = document.getElementById('jenkinsUrlSection');
    if (jenkinsSection) jenkinsSection.style.display = 'block';
    // 显示跳过指定阶段
    const skipSection = document.getElementById('skipOptionsSection');
    if (skipSection) skipSection.style.display = 'block';
    const btnNext = document.getElementById('btnNextStep5');
    if (btnNext) btnNext.style.display = 'none';
    const btnDirect = document.getElementById('btnDirectPipeline');
    if (btnDirect) btnDirect.style.display = 'inline-flex';

    _setTscanStep5Mode(false);
    toggleForm(3, 5); updateStepStatus(3, 5); currentStep = 5;
    return;
  }

  if (publishType === 'tscan') {
    // ── 独立 TSCAN 构建模式：跳转 step 4 选择历史版本 ──
    isTscanStandalone = true;
    const step4PlatformEl = document.getElementById('step4Platform');
    const step4DeviceEl = document.getElementById('step4Device');
    const step4PublishTypeEl = document.getElementById('step4PublishType');
    if (step4PlatformEl) step4PlatformEl.textContent = selectedPlatform;
    if (step4DeviceEl) step4DeviceEl.textContent = selectedDevice;
    if (step4PublishTypeEl) step4PublishTypeEl.textContent = '基于历史版本构建 TSCAN';
    toggleForm(3, 4); updateStepStatus(3, 4); currentStep = 4;
    fetchHistoryVersions(selectedDevice);
    return;
  }

  // 重置 skipJenkins 模式
  skipJenkinsMode = false;
  isTscanStandalone = false;
  _setTscanStep5Mode(false);
  const jenkinsSection = document.getElementById('jenkinsUrlSection');
  if (jenkinsSection) jenkinsSection.style.display = 'none';
  // 隐藏跳过指定阶段
  const skipSection = document.getElementById('skipOptionsSection');
  if (skipSection) skipSection.style.display = 'none';
  const btnNext = document.getElementById('btnNextStep5');
  if (btnNext) btnNext.style.display = 'inline-flex';
  const btnDirect = document.getElementById('btnDirectPipeline');
  if (btnDirect) btnDirect.style.display = 'none';

  if(publishType === 'newProject'){
    setInputPlaceholder(true);
    const paramStepPlatformEl = document.getElementById('paramStepPlatform');
    const paramStepDeviceEl = document.getElementById('paramStepDevice');
    const paramStepPublishTypeEl = document.getElementById('paramStepPublishType');
    const paramStepVersionWrapEl = document.getElementById('paramStepVersionWrap');
    const projectIdEl = document.getElementById('projectId');
    if (paramStepPlatformEl) paramStepPlatformEl.textContent = selectedPlatform;
    if (paramStepDeviceEl) paramStepDeviceEl.textContent = selectedDevice;
    if (paramStepPublishTypeEl) paramStepPublishTypeEl.textContent = publishTypeMap[publishType] || '';
    if (paramStepVersionWrapEl) paramStepVersionWrapEl.classList.add('hidden');
    const paramStepHistDetailEl = document.getElementById('paramStepHistDetailWrap');
    if (paramStepHistDetailEl) paramStepHistDetailEl.classList.add('hidden');
    clearAllInput();
    if (projectIdEl) projectIdEl.value = selectedDevice || '';
    selectWikiTokenForDevice(selectedDevice);  // 新项目模式下重新填充 Wiki Token
    setBuildParamPlaceholder();
    toggleForm(3,5); updateStepStatus(3,5); currentStep = 5;
  }else{
    setInputPlaceholder(false);
    if (wikiTokenTomSelect) wikiTokenTomSelect.clear();
    const step4PlatformEl = document.getElementById('step4Platform');
    const step4DeviceEl = document.getElementById('step4Device');
    const step4PublishTypeEl = document.getElementById('step4PublishType');
    if (step4PlatformEl) step4PlatformEl.textContent = selectedPlatform;
    if (step4DeviceEl) step4DeviceEl.textContent = selectedDevice;
    if (step4PublishTypeEl) step4PublishTypeEl.textContent = publishTypeMap[publishType] || '';
    toggleForm(3,4); updateStepStatus(3,4); currentStep = 4;
    fetchHistoryVersions(selectedDevice);
  }
}

function setInputPlaceholder(isNewProject) {
  const inputs = [
    {id:'projectStage', tip:'如 Alpha1、Beta2、RC3、OTA第三轮、OTA1 hotfix 等'},
    {id:'versionNumber', tip:'请输入版本号(4位数)，格式：x.x.x.x'},
    {id:'publishBranch', tip:'请输入或选择发版分支，如releases/stuttgart/ota'},
    {id:'docName', tip:'自动生成，无需手动输入'}
  ];
  inputs.forEach(item=>{ const dom = document.getElementById(item.id); if (dom) dom.placeholder = isNewProject ? item.tip : ''; })
}

function selectVersion(e, project, suffix){
  document.querySelectorAll('.version-item').forEach(i=>i.classList.remove('active'));
  e.classList.add('active');

  // 先用 _versionData 中的基础数据
  selectedVersionData = e._versionData || {};

  // 异步获取完整配置，填充更多字段
  if (project && suffix) {
    fetch(`/api/projects/${encodeURIComponent(project)}/version/${encodeURIComponent(suffix)}`, { cache: 'no-store' })
      .then(r => r.json())
      .then(data => {
        if (data.ok && data.data) {
          const cfg = data.data;
          const rel = cfg.release || {};
          const vars = cfg.vars || {};
          selectedVersionData = {
            ...selectedVersionData,  // 保留初始 _versionData 中的 formattedTs/variant 等
            projectId: rel.project || project,
            stage: rel.stage || selectedVersionData.stage || '',
            version: rel.version || selectedVersionData.version || '',
            branch: rel.notes || selectedVersionData.branch || '',
            docName: rel.variant || selectedVersionData.docName || '',
            feishuTemplateNodeToken: (cfg.feishu && cfg.feishu.template_node_token) || '',
            buildParams: {
              tag_algo: vars.tag || '',
              tag_boot: vars.boot_tag || '',
              tag_recovery: vars.recovery_tag || '',
              tag_fct: vars.fct_tag || '',
              ver_release: vars.release_version_name || '',
              ver_debug: vars.debug_version_name || '',
              ver_fct: vars.fct_version_name || '',
              diff_name: vars.prev_version_name || '',
              diff_code: vars.prev_version_code || '',
              build_content: vars.build_mode || '',
              fw_ver_strategy_env: vars.fw_ver_strategy_env || 'none',
              build_tscan: vars.build_tscan || 'no',
              auto_bind_after_upgrade: vars.auto_bind_after_upgrade || 'no',
              hmi_core_mm_owner_dep: vars.hmi_core_mm_owner_dep || ''
            }
          };
        }
      })
      .catch(err => console.error('获取版本详情失败:', err));
  }

  canGoNextStep4 = true; const btn = document.getElementById('toForm5Btn');
  if (btn) { btn.classList.remove('btn-disabled'); btn.classList.add('btn-primary'); }
}

async function nextStep4() {
  if(!canGoNextStep4){ alert("请先选择一个历史版本！"); return; }

  // ── 独立 TSCAN 模式：跳到简化的 step 5 ──
  if (isTscanStandalone) {
    // 填充 step 5 参数摘要
    const paramStepPlatformEl = document.getElementById('paramStepPlatform');
    const paramStepDeviceEl = document.getElementById('paramStepDevice');
    const paramStepPublishTypeEl = document.getElementById('paramStepPublishType');
    if (paramStepPlatformEl) paramStepPlatformEl.textContent = selectedPlatform;
    if (paramStepDeviceEl) paramStepDeviceEl.textContent = selectedDevice;
    if (paramStepPublishTypeEl) paramStepPublishTypeEl.textContent = '基于历史版本构建 TSCAN';
    // 填充 TAG 信息
    const b = selectedVersionData ? (selectedVersionData.buildParams || {}) : {};
    document.getElementById('projectId').value = selectedDevice || '';
    document.getElementById('tag_algo').value = b.tag_algo || '';
    document.getElementById('tag_boot').value = b.tag_boot || '';
    document.getElementById('tag_recovery').value = b.tag_recovery || '';
    document.getElementById('tag_fct').value = b.tag_fct || '';
    // 隐藏非 TAG 的表单区域
    _setTscanStep5Mode(true);
    toggleForm(4, 5); updateStepStatus(4, 5); currentStep = 5;
    return;
  }

  // 设置参数摘要显示信息
  const paramStepPlatformEl = document.getElementById('paramStepPlatform');
  const paramStepDeviceEl = document.getElementById('paramStepDevice');
  const paramStepPublishTypeEl = document.getElementById('paramStepPublishType');
  const paramStepVersionValEl = document.getElementById('paramStepVersionVal');
  const paramStepVersionWrapEl = document.getElementById('paramStepVersionWrap');
  if (paramStepPlatformEl) paramStepPlatformEl.textContent = selectedPlatform;
  if (paramStepDeviceEl) paramStepDeviceEl.textContent = selectedDevice;
  if (paramStepPublishTypeEl) paramStepPublishTypeEl.textContent = publishTypeMap[publishType] || '';
  if (selectedVersionData && selectedVersionData.version) {
    if (paramStepVersionValEl) paramStepVersionValEl.textContent = selectedVersionData.version;
    if (paramStepVersionWrapEl) paramStepVersionWrapEl.classList.remove('hidden');
  }

  // 获取所有输入元素引用
  const projectIdEl = document.getElementById('projectId');
  const projectStageEl = document.getElementById('projectStage');
  const versionNumberEl = document.getElementById('versionNumber');
  const publishBranchEl = document.getElementById('publishBranch');
  const tagAlgoEl = document.getElementById('tag_algo');
  const tagBootEl = document.getElementById('tag_boot');
  const tagRecoveryEl = document.getElementById('tag_recovery');
  const tagFctEl = document.getElementById('tag_fct');
  const verReleaseEl = document.getElementById('ver_release');
  const verDebugEl = document.getElementById('ver_debug');
  const diffNameEl = document.getElementById('diff_name');
  const diffCodeEl = document.getElementById('diff_code');

  // ★ 先加载当前设备的发版分支下拉选项（await 确保异步完成后再继续）
  await loadBranchOptions(selectedDevice);

  // ★ 再回填分支值（下拉选项已加载完毕，不会被 clearOptions 清掉）
  if (publishBranchEl && branchTomSelect) {
    let histBranch = (selectedVersionData && selectedVersionData.branch) || '';
    // 如果历史版本没有分支数据，尝试用 objectBranch 第一条作为默认值
    if (!histBranch) {
      const currentOptions = branchTomSelect.options;
      const optKeys = Object.keys(currentOptions);
      if (optKeys.length > 0) {
        histBranch = optKeys[0];  // 使用下拉框第一条分支作为默认
      }
    }
    if (histBranch) {
      branchTomSelect.addOption({ value: histBranch, text: histBranch });
      branchTomSelect.refreshOptions(false);
      branchTomSelect.setValue(histBranch);
    }
  }

  // 回填其他表单字段
  if (projectIdEl) projectIdEl.value = selectedDevice || '';
  if (selectedVersionData) {
    if (projectStageEl) projectStageEl.value = selectedVersionData.stage || '';
    if (versionNumberEl) versionNumberEl.value = (selectedVersionData.version || '').replace('v','');
    updateDocName();
    const b = selectedVersionData.buildParams || {};
    if (tagAlgoEl) tagAlgoEl.value = b.tag_algo || '';
    if (tagBootEl) tagBootEl.value = b.tag_boot || '';
    if (tagRecoveryEl) tagRecoveryEl.value = b.tag_recovery || '';
    if (tagFctEl) tagFctEl.value = b.tag_fct || '';
    if (verReleaseEl) verReleaseEl.value = b.ver_release || '';
    if (verDebugEl) verDebugEl.value = b.ver_debug || '';
    if (diffNameEl) diffNameEl.value = b.diff_name || '';
    if (diffCodeEl) diffCodeEl.value = b.diff_code || '';
    const fwVerEnvEl = document.getElementById('fw_ver_strategy_env'); if (fwVerEnvEl) fwVerEnvEl.value = b.fw_ver_strategy_env || 'none';
    const tscanEl = document.getElementById('build_tscan'); if (tscanEl) tscanEl.checked = b.build_tscan === 'yes';
    const autoBindEl = document.getElementById('auto_bind_after_upgrade'); if (autoBindEl) autoBindEl.checked = b.auto_bind_after_upgrade === 'yes';
    const hmiDepEl = document.getElementById('hmi_core_mm_owner_dep'); if (hmiDepEl) hmiDepEl.value = b.hmi_core_mm_owner_dep || '';
    initBuildContentCheckboxes(b.build_content || ''); updateBuildContentInput();
  }
  setBuildParamPlaceholder(); resetEditState();
  _setTscanStep5Mode(false);  // 非 TSCAN 模式确保隐藏 TSCAN 按钮
  toggleForm(4,5); updateStepStatus(4,5); currentStep = 5;
  if (selectedVersionData && selectedVersionData.feishuTemplateNodeToken) {
    if (wikiTokenTomSelect) {
      // 检查 token 是否已在 options 中
      const token = selectedVersionData.feishuTemplateNodeToken;
      const existing = Object.entries(template_node_token).find(([k, v]) => v === token);
      if (existing) {
        wikiTokenTomSelect.setValue(token);
      } else {
        // token 不在预设列表中，添加为自定义选项然后选中
        wikiTokenTomSelect.addOption({ value: token, text: selectedDevice || '自定义' });
        wikiTokenTomSelect.setValue(token);
      }
    }
  } else if (wikiTokenTomSelect) {
    selectWikiTokenForDevice(selectedDevice);
  }
  fillHistoryVersionDetail('paramStep');
}

// ================== 发版分支 ：TomSelect 下拉 + 输入 + 校验 + 保存 ==================
let branchTomSelect = null;

function getBranchValue() {
  if (branchTomSelect) return branchTomSelect.getValue() || '';
  const el = document.getElementById('publishBranch');
  return el ? el.value.trim() : '';
}

function initBranchSelect() {
  const el = document.getElementById('publishBranch');
  if (!el) return;
  if (branchTomSelect) {
    branchTomSelect.destroy();
    branchTomSelect = null;
  }
  branchTomSelect = new TomSelect('#publishBranch', {
    options: [],
    placeholder: '选择或输入发版分支...',
    maxOptions: null,
    create: true,
    createFilter: function(input) {
      return input.trim().length > 0;
    },
    createOnBlur: true,
    persist: false,
    highlight: true,
    allowEmptyOption: true,
    render: {
      option: function(data, escape) {
        return '<div><span style="color:#1a1a1a;font-weight:500;">' + escape(data.text) + '</span></div>';
      },
      item: function(data, escape) {
        return '<div><span style="color:#1a1a1a;font-weight:500;">' + escape(data.text) + '</span></div>';
      },
      no_results: function(data, escape) {
        return '<div class="no-results">无匹配历史分支，可直接输入新分支名</div>';
      }
    },
    onChange: function(value) {
      handleInputChange({ id: 'publishBranch', value: value });
      validateBranchName();
    },
  });
}

async function loadBranchOptions(device, clearValue) {
  if (!device || !branchTomSelect) return;
  // 清空已有选项
  branchTomSelect.clearOptions();
  // 切换项目时清除旧值
  if (clearValue) {
    branchTomSelect.clear();
  }
  try {
    const resp = await fetch('/api/config/object-branches?project=' + encodeURIComponent(device));
    const data = await resp.json();
    if (data.ok && data.branches && data.branches.length > 0) {
      data.branches.forEach(function(b) {
        branchTomSelect.addOption({ value: b, text: b });
      });
    }
    // 刷新选项列表
    branchTomSelect.refreshOptions(false);
  } catch (e) {
    console.error('加载发版分支失败:', e);
  }
}

function validateBranchName() {
  if (!branchTomSelect) return true;
  const val = (branchTomSelect.getValue() || '').trim();
  if (!val) return true;
  const device = selectedDevice || document.getElementById('projectId')?.value.trim() || '';
  const required = ['releases', 'feature', 'dev', 'master'];
  if (device) required.push(device);
  const ok = required.some(function(kw) { return val.toLowerCase().includes(kw.toLowerCase()); });
  if (!ok) {
    // TomSelect 外框标红
    const wrapper = document.getElementById('publishBranch')?.closest('.ts-wrapper');
    if (wrapper) wrapper.style.borderColor = '#e24b4a';
  } else {
    const wrapper = document.getElementById('publishBranch')?.closest('.ts-wrapper');
    if (wrapper) wrapper.style.borderColor = '';
  }
  return ok;
}

async function saveBranchToConfig(device, branch) {
  if (!device || !branch) return;
  try {
    await fetch('/api/config/add-object-branch', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ project: device, branch: branch }),
    });
  } catch (e) {
    console.error('保存发版分支失败:', e);
  }
}

// ── TSCAN 独立模式 step 5 控制（两个容器互斥，不管按钮）──
function _setTscanStep5Mode(enable) {
  const tscanWrap = document.getElementById('tscanStep5TscanWrap');
  const normalWrap = document.getElementById('tscanStep5NormalWrap');

  if (enable) {
    if (tscanWrap) tscanWrap.style.display = '';
    if (normalWrap) normalWrap.style.display = 'none';
    const b = selectedVersionData ? (selectedVersionData.buildParams || {}) : {};
    const ver = selectedVersionData ? (selectedVersionData.version || '').replace('v','') : '';
    const setVal = (id, val) => { const el = document.getElementById(id); if (el) el.value = val || ''; };

    // 来源标题：cologne OTA4 第一轮 v3.17.0.1 — 26/05/28
    const srcEl = document.getElementById('tscanStep5Source');
    if (srcEl && selectedVersionData) {
      const docName = selectedVersionData.docName || '';
      const ts = selectedVersionData.timestamp || '';
      const formattedTs = ts.length >= 8 ? ts.substring(2,4) + '/' + ts.substring(4,6) + '/' + ts.substring(6,8) : '';
      srcEl.textContent = docName + (formattedTs ? ' — ' + formattedTs : '');
    }

    setVal('tscanStep5ProjectId', selectedDevice || '');
    setVal('tscanStep5Version', ver);
    setVal('tscanStep5TagAlgo', b.tag_algo);
    setVal('tscanStep5TagBoot', b.tag_boot);
    setVal('tscanStep5TagRecovery', b.tag_recovery);
    setVal('tscanStep5TagFct', b.tag_fct);
    setVal('tscanStep5BuildMode', b.build_content || 'OTA,BOOT,RECOVERY');
    setVal('tscanStep5FctVer', ver.split('.').slice(0,3).join('.') || '--');
    setVal('tscanStep5VersionName', ver.split('.').slice(0,3).join('.') || '--');
  } else {
    if (tscanWrap) tscanWrap.style.display = 'none';
    if (normalWrap) normalWrap.style.display = '';
  }
}

async function startTscanBuildFromStep5() {
  const device = selectedDevice;
  if (!device) { alert('请先选择目标项目'); return; }

  // 从可编辑输入框读取值（允许用户修改）
  const getVal = (id, fallback) => {
    const el = document.getElementById(id);
    return (el && el.value) ? el.value.trim() : fallback;
  };
  const b = selectedVersionData ? (selectedVersionData.buildParams || {}) : {};
  const ver = selectedVersionData ? (selectedVersionData.version || '').replace('v','') : '';

  const tagAlgo = getVal('tscanStep5TagAlgo', b.tag_algo || '');
  if (!tagAlgo) { alert('算法 tag 不能为空'); return; }

  const payload = {
    device: device,
    project_id: getVal('tscanStep5ProjectId', selectedDevice),
    version: getVal('tscanStep5Version', ver),
    tag_algo: tagAlgo,
    tag_boot: getVal('tscanStep5TagBoot', b.tag_boot || ''),
    tag_recovery: getVal('tscanStep5TagRecovery', b.tag_recovery || ''),
    tag_fct: getVal('tscanStep5TagFct', b.tag_fct || ''),
    build_mode: getVal('tscanStep5BuildMode', b.build_content || 'OTA,BOOT,RECOVERY'),
    build_test_tool: getVal('tscanStep5TestTool', 'NO') === '是' ? 'YES' : 'NO',
    fct_version_name: getVal('tscanStep5FctVer', ver.split('.').slice(0,3).join('.')),
    version_name: getVal('tscanStep5VersionName', ver.split('.').slice(0,3).join('.')),
    build_release: getVal('tscanStep5BuildRelease', 'release'),
    tscancode_check: 'YES',
    platform: selectedPlatform,
    user_id: currentUserId,
    jenkins_auth: getJenkinsAuth(),
    jenkins_job_url: (platformJenkinsJobUrl && platformJenkinsJobUrl[selectedPlatform]) || '',
  };

  try {
    const resp = await fetch(`/api/projects/${encodeURIComponent(device)}/tscan-only`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await resp.json();
    if (!data.ok) { alert('启动 TSCAN 构建失败: ' + (data.error || '未知错误')); return; }
    const taskId = data.task_id;
    // 重置进度步骤为 TSCAN 专用
    compileSteps = [
      { name: 'TSCAN构建', status: 'waiting', percent: 0 },
    ];
    totalPercent = 0;
    _stepUIDirty = true;
    document.getElementById('currentTaskTitle').textContent = 'TSCAN: ' + taskId;
    document.getElementById('currentTaskTitle').style.display = '';
    const flCard = document.getElementById('feishuLinkCard');
    if (flCard) flCard.classList.add('hidden');
    const tscanSection = document.getElementById('tscanProgressSection');
    if (tscanSection) tscanSection.classList.add('hidden'); // 独立模式不显示子进度
    // 隐藏总进度条（TSCAN 独立模式只有一步）
    const totalWrap = document.getElementById('totalProgressWrap');
    if (totalWrap) totalWrap.classList.add('hidden');
    const logBox = document.getElementById('compileLogBox');
    if (logBox) logBox.innerHTML = '';
    logList = [];
    renderCompileProgress();
    hideAllStopButtons();

    toggleForm(5, 8);
    updateStepStatus(5, 8);
    // prevStepBefore8 = currentStep;  // [2026-06-02] 注释
    currentStep = 8;
    startTaskListPolling();
    startCompileTask(taskId, 'tscan');
  } catch (err) {
    alert('网络错误: ' + err.message);
  }
}

function updateDocName() {
  const pidEl = document.getElementById('projectId');
  const stageEl = document.getElementById('projectStage');
  const verEl = document.getElementById('versionNumber');
  const docNameInput = document.getElementById('docName');

  if (!pidEl || !stageEl || !verEl || !docNameInput) return;

  const pid = pidEl.value.trim();
  const stage = stageEl.value.trim();
  const ver = verEl.value.trim();

  if (pid && stage && ver) {
    docNameInput.value = `${pid} ${stage} v${ver}`;
  } else {
    docNameInput.value = '';
  }
  docNameInput.classList.remove('edited');
}

function nextStep5() {
  const pidEl = document.getElementById('projectId'); const stageEl = document.getElementById('projectStage');
  const verEl = document.getElementById('versionNumber'); const branchEl = document.getElementById('publishBranch');
  if (!pidEl || !stageEl || !verEl || !branchEl) { alert('关键输入框未找到，请刷新页面重试'); return; }
  const pid = pidEl.value.trim(); const stage = stageEl.value.trim();
  const ver = verEl.value.trim(); const branch = getBranchValue();
  if (!pid) { alert('请填写项目标识'); return; }
  if (!stage) { alert('请填写项目阶段'); return; }
  if (!ver) { alert('请填写版本号'); return; }
  if (!versionRegex.test(ver)) { alert('版本号格式不正确，请输入 x.x.x.x 格式（如 1.0.0.1）'); return; }
  if (!branch) { alert('请填写发版分支'); return; }
  if (!validateBranchName()) { alert('发版分支格式不正确，必须包含 releases/feature/dev/master 或当前项目名'); return; }
  updateDocName();
  const docNameEl = document.getElementById('docName'); const doc = docNameEl ? docNameEl.value : '';
  const buildStepPlatformEl = document.getElementById('buildStepPlatform');
  const buildStepDeviceEl = document.getElementById('buildStepDevice');
  const buildStepProjectIdEl = document.getElementById('buildStepProjectId');
  const buildStepProjectStageEl = document.getElementById('buildStepProjectStage');
  const buildStepVersionNumberEl = document.getElementById('buildStepVersionNumber');
  const buildStepPublishBranchEl = document.getElementById('buildStepPublishBranch');
  const buildStepDocNameEl = document.getElementById('buildStepDocName');
  if (buildStepPlatformEl) buildStepPlatformEl.textContent = selectedPlatform;
  if (buildStepDeviceEl) buildStepDeviceEl.textContent = selectedDevice;
  if (buildStepProjectIdEl) buildStepProjectIdEl.textContent = pid;
  if (buildStepProjectStageEl) buildStepProjectStageEl.textContent = stage;
  if (buildStepVersionNumberEl) buildStepVersionNumberEl.textContent = ver;
  if (buildStepPublishBranchEl) buildStepPublishBranchEl.textContent = branch;
  if (buildStepDocNameEl) buildStepDocNameEl.textContent = doc;
  const wikiTokenEl5 = document.getElementById('wiki_token');
  const buildStepWikiTokenEl = document.getElementById('buildStepWikiToken');
  if (buildStepWikiTokenEl && wikiTokenEl5) buildStepWikiTokenEl.textContent = wikiTokenEl5.value.trim() || '--';

  // 标记与历史版本相比有变更的字段（红色加粗）
  const isNewProject = publishType === 'newProject';
  const orig = selectedVersionData || {};
  const origStage = orig.stage || '';
  const origVersion = (orig.version || '').replace('v','');
  const origBranch = orig.branch || '';
  function markIfChanged(el, curVal, origVal) {
    if (!el) return;
    if (isNewProject || (origVal && curVal !== origVal)) {
      el.className = 'history-value value-changed';
    } else {
      el.className = 'history-value';
    }
  }
  markIfChanged(buildStepProjectStageEl, stage, origStage);
  markIfChanged(buildStepVersionNumberEl, ver, origVersion);
  markIfChanged(buildStepPublishBranchEl, branch, origBranch);
  markIfChanged(buildStepDocNameEl, doc, orig.docName || '');

  toggleForm(5,6); updateStepStatus(5,6); currentStep = 6;
  updateVersionModeHint();  // 初始化版本号提示
  fetchDiffVersions(selectedDevice);
  fillHistoryVersionDetail('buildStep');
}

function initBuildContentCheckboxes(initialValue) {
  const checkboxContainer = document.getElementById('buildContentCheckboxes'); if (!checkboxContainer) return;
  checkboxContainer.innerHTML = '';
  const selectedValues = initialValue ? initialValue.split(',').map(v => v.trim()) : [];
  buildContentOptions.forEach(option => {
    const checkboxItem = document.createElement('div'); checkboxItem.className = 'checkbox-item';
    const isChecked = selectedValues.length > 0 ? selectedValues.includes(option.value) : option.defaultChecked;
    checkboxItem.innerHTML = `<input type="checkbox" id="buildContent_${option.value}" value="${option.value}" ${isChecked ? 'checked' : ''} onchange="updateBuildContentInput()"><label for="buildContent_${option.value}">${option.label}</label>`;
    checkboxContainer.appendChild(checkboxItem);
  });
  updateBuildContentInput();
}

function updateBuildContentInput() {
  const checkedValues = [];
  buildContentOptions.forEach(option => {
    const checkbox = document.getElementById(`buildContent_${option.value}`);
    if (checkbox && checkbox.checked) checkedValues.push(option.value);
  });
  const buildContentInput = document.getElementById('build_content');
  if (buildContentInput) { buildContentInput.value = checkedValues.join(','); handleInputChange(buildContentInput); }
}

function checkThreePartVersionFormat(elId) {
  const el = document.getElementById(elId); const errorTipId = `${elId}ErrorTip`;
  const errorTip = document.getElementById(errorTipId); if (!el || !errorTip) return;
  const value = el.value.trim(); errorTip.classList.toggle('show', value && !versionThreePartRegex.test(value));
}

// 检测 Release/Debug 版本号是否一致，更新提示
function updateVersionModeHint() {
  const verRelease = (document.getElementById('ver_release')?.value || '').trim();
  const verDebug = (document.getElementById('ver_debug')?.value || '').trim();
  const hintEl = document.getElementById('versionModeHintText');
  const hintWrap = document.getElementById('versionModeHint');
  if (!hintEl || !hintWrap) return;

  if (verRelease && verDebug && verRelease === verDebug) {
    // 版本号一样 → 新版本号规则 pairRule
    hintEl.innerHTML = '<span style="color:#e67e22;">新版本号规则 pairRule=true</span>：release、debug 版本号的第四位成双成对（偶数 = debug，奇数 = release）。构建为异步并行。';
    hintWrap.style.color = '#e67e22';
  } else if (verRelease && verDebug) {
    // 版本号不一样 → 老项目规则
    hintEl.innerHTML = '<span style="color:#e67e22;">老项目规则</span>：debug 版本号第三位比 release 版本号高一位（第四位相同）。构建为异步并行。';
    hintWrap.style.color = '#e67e22';
  } else {
    hintEl.textContent = 'Release 与 Debug 将同时触发构建（异步并行）。';
    hintWrap.style.color = '#94a3b8';
  }
}

// 打开 Gerrit 查看发版分支最新 tag
function openGerritBranchLog() {
  const branchEl = document.getElementById('publishBranch');
  let branch = branchEl ? branchEl.value.trim() : '';
  if (!branch) {
    alert('请先在第五步填写发版分支');
    return;
  }
  // 确保路径以 refs/heads/ 开头
  if (!branch.startsWith('refs/heads/')) {
    branch = 'refs/heads/' + branch;
  }
  const url = 'https://gerrit.huami.com/gitweb/?p=firmware/huamisys/manifest.git;a=shortlog;h=' + encodeURIComponent(branch);
  window.open(url, '_blank');
}

// 打开 Gerrit 查看指定类型的 tag（bootloader / recovery / fct）
function openGerritTagFilter(suffix) {
  // 使用项目标识（设备名）作为 Gerrit tag 过滤前缀
  const device = selectedDevice;
  if (!device) {
    alert('请先在第二步选择目标项目，以便生成过滤条件');
    return;
  }
  const filter = device + '-' + suffix;
  const url = 'https://gerrit.huami.com/admin/repos/firmware/huamisys/manifest,tags/q/filter:' + encodeURIComponent(filter);
  window.open(url, '_blank');
}

function nextStep6() {
  const verReleaseEl = document.getElementById('ver_release'); const verDebugEl = document.getElementById('ver_debug');
  if (!verReleaseEl || !verDebugEl) { alert('版本参数输入框未找到，请刷新页面重试'); return; }
  const verRelease = verReleaseEl.value.trim(); const verDebug = verDebugEl.value.trim();
  let hasError = false;
  const verReleaseErrorTip = document.getElementById('ver_releaseErrorTip');
  const verDebugErrorTip = document.getElementById('ver_debugErrorTip');
  if (verRelease && !versionThreePartRegex.test(verRelease) && verReleaseErrorTip) { verReleaseErrorTip.classList.add('show'); hasError = true; }
  else if (verReleaseErrorTip) verReleaseErrorTip.classList.remove('show');
  if (verDebug && !versionThreePartRegex.test(verDebug) && verDebugErrorTip) { verDebugErrorTip.classList.add('show'); hasError = true; }
  else if (verDebugErrorTip) verDebugErrorTip.classList.remove('show');
  if (hasError) { alert('版本参数格式错误，请输入 x.x.x 格式（如 1.3.0）'); return; }

  // ── 第六步必填校验：TAG / 版本参数 / 构建内容 ──
  const tagAlgo = document.getElementById('tag_algo')?.value.trim() || '';
  const tagBoot = document.getElementById('tag_boot')?.value.trim() || '';
  const tagRecovery = document.getElementById('tag_recovery')?.value.trim() || '';
  const tagFct = document.getElementById('tag_fct')?.value.trim() || '';
  const buildContent = document.getElementById('build_content')?.value.trim() || '';

  const missing = [];
  if (!tagAlgo) missing.push('算法 tag');
  if (!tagBoot) missing.push('boot_tag');
  if (!tagRecovery) missing.push('recovery_tag');
  if (!tagFct) missing.push('fct_tag');
  if (!verRelease) missing.push('release 版本号');
  if (!verDebug) missing.push('debug 版本号');
  if (!buildContent) missing.push('构建内容（请至少勾选一项）');

  if (missing.length > 0) {
    alert('请完善以下必填项：\n' + missing.map(m => '• ' + m).join('\n'));
    return;
  }
  const final7PlatformEl = document.getElementById('final7Platform'); const final7DeviceEl = document.getElementById('final7Device');
  const final7PublishTypeEl = document.getElementById('final7PublishType'); const final7ProjectIdEl = document.getElementById('final7ProjectId');
  const final7ProjectStageEl = document.getElementById('final7ProjectStage'); const final7VersionNumberEl = document.getElementById('final7VersionNumber');
  const final7PublishBranchEl = document.getElementById('final7PublishBranch'); const final7DocNameEl = document.getElementById('final7DocName');
  const final7VersionWrapEl = document.getElementById('final7VersionWrap'); const final7VersionEl = document.getElementById('final7Version');
  if (final7PlatformEl) final7PlatformEl.textContent = selectedPlatform; if (final7DeviceEl) final7DeviceEl.textContent = selectedDevice;
  if (final7PublishTypeEl) final7PublishTypeEl.textContent = publishTypeMap[publishType] || '';
  if (final7ProjectIdEl) final7ProjectIdEl.textContent = document.getElementById('projectId')?.value.trim() || '';

  // 基础信息字段（阶段/版本号/分支/文档名）与历史版本比对，变更标红加粗
  const origBase = selectedVersionData || {};
  setValueWithDiff('final7ProjectStage', document.getElementById('projectStage')?.value.trim() || '', origBase.stage || '');
  setValueWithDiff('final7VersionNumber', document.getElementById('versionNumber')?.value.trim() || '', (origBase.version || '').replace('v',''));
  setValueWithDiff('final7PublishBranch', getBranchValue(), origBase.branch || '');
  setValueWithDiff('final7DocName', document.getElementById('docName')?.value.trim() || '', origBase.docName || '');
  const final7WikiTokenEl = document.getElementById('final7WikiToken');
  if (final7WikiTokenEl) { final7WikiTokenEl.textContent = document.getElementById('wiki_token')?.value.trim() || '--'; }

  if(selectedVersionData && final7VersionWrapEl && final7VersionEl){ final7VersionWrapEl.classList.remove('hidden'); final7VersionEl.textContent = selectedVersionData.version || ''; }
  const origin = selectedVersionData ? selectedVersionData.buildParams || {} : {};
  const curr = {
    tag_algo: document.getElementById('tag_algo')?.value || '', tag_boot: document.getElementById('tag_boot')?.value || '',
    tag_recovery: document.getElementById('tag_recovery')?.value || '', tag_fct: document.getElementById('tag_fct')?.value || '',
    ver_release: verReleaseEl?.value || '', ver_debug: verDebugEl?.value || '',
    ver_fct: verReleaseEl?.value || '',  // fct 版本号始终等于 release 版本号
    diff_name: document.getElementById('diff_name')?.value || '', diff_code: document.getElementById('diff_code')?.value || '',
    build_content: document.getElementById('build_content')?.value || '',
    fw_ver_strategy_env: document.getElementById('fw_ver_strategy_env')?.value || 'none',
    build_tscan: document.getElementById('build_tscan')?.checked ? 'yes' : 'no',
    auto_bind_after_upgrade: document.getElementById('auto_bind_after_upgrade')?.checked ? 'yes' : 'no',
    hmi_core_mm_owner_dep: document.getElementById('hmi_core_mm_owner_dep')?.value || ''
  };
  setValueWithDiff('final7TagAlgo', curr.tag_algo, origin.tag_algo);
  setValueWithDiff('final7TagBoot', curr.tag_boot, origin.tag_boot);
  setValueWithDiff('final7TagRecovery', curr.tag_recovery, origin.tag_recovery);
  setValueWithDiff('final7TagFct', curr.tag_fct, origin.tag_fct);
  setValueWithDiff('final7VerRelease', curr.ver_release, origin.ver_release);
  setValueWithDiff('final7VerDebug', curr.ver_debug, origin.ver_debug);
  setValueWithDiff('final7VerFct', curr.ver_fct, origin.ver_fct);
  setValueWithDiff('final7DiffName', curr.diff_name, origin.diff_name);
  setValueWithDiff('final7DiffCode', curr.diff_code, origin.diff_code);
  setValueWithDiff('final7BuildContent', curr.build_content, origin.build_content);
  setValueWithDiff('final7FwVerStrategyEnv', curr.fw_ver_strategy_env, origin.fw_ver_strategy_env);
  setValueWithDiff('final7BuildTscan', curr.build_tscan === 'yes' ? '是' : '否', (origin.build_tscan === 'yes' ? '是' : '否'));
  setValueWithDiff('final7AutoBind', curr.auto_bind_after_upgrade === 'yes' ? '是' : '否', (origin.auto_bind_after_upgrade === 'yes' ? '是' : '否'));
  // HMI_CORE_MM_OWNER_DEP：默认 2（release=2、debug=2）；用户输入 N（>0）则 release=N、debug=N×2
  function formatHmiDep(raw) {
    const s = (raw || '').trim();
    const n = (!s || s === '2') ? 2 : (parseInt(s, 10) || 2);
    return { release: String(n), debug: (n === 2 ? '2' : String(n * 2)) };
  }
  const hmiCurr = formatHmiDep(curr.hmi_core_mm_owner_dep);
  const hmiOrigin = formatHmiDep(origin.hmi_core_mm_owner_dep);
  setValueWithDiff('final7HmiRelease', hmiCurr.release, hmiOrigin.release);
  setValueWithDiff('final7HmiDebug', hmiCurr.debug, hmiOrigin.debug);
  toggleForm(6,7); updateStepStatus(6,7); currentStep = 7;
}

function setValueWithDiff(elId, currVal, originVal) {
  const el = document.getElementById(elId); if (!el) return;
  el.textContent = currVal || '--'; el.className = currVal !== originVal ? 'step7-value changed-value' : 'step7-value original-value';
}

let inputTimer;
function handleInputChange(el) {
  if (!el) return; clearTimeout(inputTimer);
  inputTimer = setTimeout(()=>{
    if (el.id === 'docName' || el.id === 'projectId') return;
    let curVal = el.value;
    if (el.type === 'checkbox') curVal = el.checked ? 'yes' : 'no';
    const originVal = selectedVersionData ? (selectedVersionData[el.id] || selectedVersionData.buildParams?.[el.id] || '') : '';
    el.classList.toggle('edited', curVal !== originVal);
    if (['projectStage', 'versionNumber'].includes(el.id)) updateDocName();
  }, 10);
}

function resetEditState() { document.querySelectorAll('.param-input, .build-input, .custom-select, input[type="checkbox"]').forEach(el => el.classList.remove('edited')); document.querySelectorAll('.error-tip').forEach(t => t.classList.remove('show')); }

function prevStep() {
  if(currentStep === 2){ toggleForm(2,1); updateStepStatus(2,1); currentStep=1; }
  if(currentStep === 3){ toggleForm(3,2); updateStepStatus(3,2); currentStep=2; }
  if(currentStep === 4){ isTscanStandalone = false; _setTscanStep5Mode(false); toggleForm(4,3); updateStepStatus(4,3); currentStep=3; }
  if(currentStep === 5){
    if (isTscanStandalone) {
      // 重置 TSCAN 独立模式
      _setTscanStep5Mode(false);
      isTscanStandalone = false;
      toggleForm(5, 4); updateStepStatus(5, 4); currentStep = 4;
    } else if (skipJenkinsMode) {
      // 重置 skipJenkins 模式
      skipJenkinsMode = false;
      const jenkinsSection = document.getElementById('jenkinsUrlSection');
      if (jenkinsSection) jenkinsSection.style.display = 'none';
      // 隐藏跳过指定阶段
      const skipSection = document.getElementById('skipOptionsSection');
      if (skipSection) skipSection.style.display = 'none';
      const btnNext = document.getElementById('btnNextStep5');
      if (btnNext) btnNext.style.display = 'inline-flex';
      const btnDirect = document.getElementById('btnDirectPipeline');
      if (btnDirect) btnDirect.style.display = 'none';
      toggleForm(5, 3); updateStepStatus(5, 3); currentStep = 3;
    } else {
      toggleForm(5, publishType === 'oldProject' ? 4 : 3);
      updateStepStatus(5, publishType === 'oldProject' ? 4 : 3);
      currentStep = publishType === 'oldProject' ? 4 : 3;
    }
  }
  if(currentStep === 6){ toggleForm(6,5); updateStepStatus(6,5); currentStep=5; }
  if(currentStep === 7){ toggleForm(7,6); updateStepStatus(7,6); currentStep=6; updateVersionModeHint(); }
  if(currentStep === 8){ stopTaskListPolling(); stopCurrentPolling(); toggleForm(8,7); updateStepStatus(8,7); currentStep=7; resetStopButton(); }
}

// 点击顶部步骤指示器跳转（仅允许跳转到已完成的步骤或当前步骤）
function goToStep(target) {
  if (target === currentStep) return;  // 已在当前步骤

  // 第八步需要提交任务才能进入，不能直接点击跳入
  if (target === 8 && currentStep !== 8) {
    alert('请通过第七步提交任务进入编译页面');
    return;
  }

  // 第八步不能直接跳转离开，需先停止任务
  if (currentStep === 8) {
    if (!confirm('当前任务正在执行中，确定要离开编译页面吗？任务将继续在后台运行。')) return;
    stopTaskListPolling();
    stopCurrentPolling();
    resetStopButton();
  }

  // 只能跳回到已完成步骤（target < currentStep）
  if (target > currentStep) {
    alert(`请先完成第 ${currentStep} 步`);
    return;
  }

  // 执行跳转
  toggleForm(currentStep, target);
  updateStepStatus(currentStep, target);
  currentStep = target;
}

let deviceTomSelect = null;

function fillDeviceList(p) {
  const s = document.getElementById('deviceSelect'); if (!s) return false;
  const devices = platformDevices[p] || [];

  if (!devices.length) {
    alert('未找到该平台的目标项目列表。');
    return false;
  }

  if (deviceTomSelect) {
    deviceTomSelect.destroy();
    deviceTomSelect = null;
  }

  // 清除原始 select 中的残留 option（TomSelect create 会把自定义值写入 option 标签）
  s.innerHTML = '<option value=""></option>';

  const options = devices.map(d => ({ value: d, text: d }));
  deviceTomSelect = new TomSelect('#deviceSelect', {
    options: options,
    placeholder: '选择或输入目标项目...',
    maxOptions: null,
    create: true,
    createOnBlur: true,
    createFilter: function(input) {
      // 去掉首尾空格，只允许字母、数字、下划线、连字符
      const v = input.trim();
      if (!v) return false;
      if (!/^[a-zA-Z0-9_-]+$/.test(v)) return false;
      // 不允许重复已存在的选项
      if (devices.includes(v)) return false;
      return true;
    },
    highlight: true,
    allowEmptyOption: false,
    render: {
      no_results: function(data, escape) {
        return '<div class="no-results">未找到匹配的目标项目（可直接输入新名称）</div>';
      },
      option_create: function(data, escape) {
        return '<div class="create">新增目标项目 <strong>' + escape(data.input) + '</strong>&hellip;</div>';
      }
    }
  });

  return true;
}

// ================== Wiki Token 可搜索下拉框 ==================
let wikiTokenTomSelect = null;

function initWikiTokenSelect() {
  const el = document.getElementById('wiki_token');
  if (!el) return;

  // 从 platform_config.js 构建选项列表（device_name: token）
  const options = Object.entries(template_node_token).map(([device, token]) => ({
    value: token,
    text: device,  // 显示设备名，value 是 token
  }));

  if (wikiTokenTomSelect) {
    wikiTokenTomSelect.destroy();
    wikiTokenTomSelect = null;
  }

  wikiTokenTomSelect = new TomSelect('#wiki_token', {
    options: options,
    placeholder: '输入或选择 Wiki Token...',
    maxOptions: null,
    create: true,
    createFilter: function(input) {
      // 允许创建任意非空输入
      return input.trim().length > 0;
    },
    highlight: true,
    allowEmptyOption: true,
    render: {
      option: function(data, escape) {
        // 显示格式：设备名（黑色） token值（灰色）
        return `<div><span style="color:#1a1a1a;font-weight:500;">${escape(data.text)}</span> <span style="color:#94a3b8;font-size:0.85em;">${escape(data.value)}</span></div>`;
      },
      item: function(data, escape) {
        // 选中后显示：设备名（黑色） token值（灰色小字）
        return `<div><span style="color:#1a1a1a;font-weight:500;">${escape(data.text)}</span> <span style="color:#94a3b8;font-size:0.8em;margin-left:0.5em;">${escape(data.value)}</span></div>`;
      },
      no_results: function(data, escape) {
        return '<div class="no-results">无匹配，可输入自定义 Token</div>';
      }
    },
    onItemAdd: function(value, item) {
      // 触发 handleInputChange 以跟踪变更
      const inputEl = document.getElementById('wiki_token');
      if (inputEl) handleInputChange(inputEl);
    },
  });
}

function selectWikiTokenForDevice(device) {
  // 根据当前设备自动选择匹配的 Wiki Token
  if (!wikiTokenTomSelect) return;
  const token = template_node_token[device];
  if (token) {
    wikiTokenTomSelect.setValue(token);
  } else {
    wikiTokenTomSelect.clear();
  }
}

async function saveWikiTokenIfNew() {
  // 流水线成功后：如果当前设备在 template_node_token 中没有预设 Token，
  // 但用户填写了 Token，则自动保存到 platform_config.js
  const device = selectedDevice;
  if (!device || !wikiTokenTomSelect) return;

  const currentToken = wikiTokenTomSelect.getValue();
  if (!currentToken || typeof currentToken !== 'string') return;

  // 如果已有预设 token 且匹配，无需写入
  if (template_node_token[device] === currentToken) return;

  // 调用后端 API 写入 platform_config.js
  try {
    const resp = await fetch('/api/config/update-wiki-token', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ device, token: currentToken }),
    });
    const result = await resp.json();
    if (result.ok) {
      // 更新内存中的映射
      template_node_token[device] = currentToken;
      console.log('[WikiToken] 已保存:', device, '→', currentToken);
    }
  } catch (err) {
    console.warn('[WikiToken] 保存失败:', err.message);
  }
}

async function savePlatformDeviceIfNew() {
  // 流水线成功后：如果当前设备不在 platformDevices 中（即用户手动输入的新设备），
  // 则自动写入 platform_config.js
  const device = selectedDevice;
  const platform = selectedPlatform;
  if (!device || !platform) return;

  // 检查是否为新设备（不在当前平台列表中）
  const existingDevices = platformDevices[platform] || [];
  if (existingDevices.includes(device)) return;

  // 调用后端 API 写入 platform_config.js
  try {
    const resp = await fetch('/api/config/add-platform-device', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ platform, device }),
    });
    const result = await resp.json();
    if (result.ok) {
      // 更新内存中的映射，下次选择时就能看到了
      if (!platformDevices[platform]) {
        platformDevices[platform] = [];
      }
      platformDevices[platform].push(device);
      console.log('[PlatformDevice] 已保存:', platform, '→', device);
    }
  } catch (err) {
    console.warn('[PlatformDevice] 保存失败:', err.message);
  }
}

async function saveBranchIfNew() {
  const device = selectedDevice || document.getElementById('projectId')?.value.trim();
  const branch = getBranchValue();
  if (!device || !branch) return;

  // 检查是否为新分支（不在当前 objectBranch 列表中）
  try {
    const resp = await fetch(`/api/config/object-branches?project=${encodeURIComponent(device)}`);
    const data = await resp.json();
    if (data.ok && data.branches && data.branches.includes(branch)) return;  // 已存在，跳过
  } catch (e) {
    console.warn('[BranchSave] 检查分支失败:', e.message);
    return;
  }

  // 调用后端 API 写入 platform_config.js
  try {
    const resp = await fetch('/api/config/add-object-branch', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ project: device, branch: branch }),
    });
    const result = await resp.json();
    if (result.ok && result.added) {
      console.log('[BranchSave] 已保存发版分支:', device, '→', branch);
      // 刷新下拉选项
      loadBranchOptions(device);
    }
  } catch (err) {
    console.warn('[BranchSave] 保存失败:', err.message);
  }
}

// ================== Changelog 版本差分 ==================

let clPrevTomSelect = null;
let clCurrTomSelect = null;
let clPrevData = null;   // { versionName, versionCode, branch } — 旧版本
let clCurrData = null;   // { versionName, versionCode, branch } — 新版本
let clAllOptions = [];   // 所有版本选项（用于联动过滤）
let clActiveTaskId = null;
let clEventSource = null;

function openChangelogModal() {
  // 填充置灰字段
  document.getElementById('clDevice').value = selectedDevice || '';
  document.getElementById('clManifest').value = (selectedDevice || '') + '.xml';
  document.getElementById('clAfterTag').value = '';

  // 重置
  clPrevData = null; clCurrData = null; clAllOptions = [];
  document.getElementById('btnStartChangelog').disabled = true;
  document.getElementById('changelogRequiredHint').style.display = '';

  // 显示弹框
  document.getElementById('changelogOverlay').classList.add('show');

  // 加载版本下拉
  _initChangelogVersionSelects();
}

function closeChangelogModal() {
  document.getElementById('changelogOverlay').classList.remove('show');
  if (clPrevTomSelect) { clPrevTomSelect.destroy(); clPrevTomSelect = null; }
  if (clCurrTomSelect) { clCurrTomSelect.destroy(); clCurrTomSelect = null; }
  clAllOptions = [];
}

async function _initChangelogVersionSelects() {
  const device = selectedDevice;
  if (!device) return;

  // 销毁旧实例
  if (clPrevTomSelect) { clPrevTomSelect.destroy(); clPrevTomSelect = null; }
  if (clCurrTomSelect) { clCurrTomSelect.destroy(); clCurrTomSelect = null; }
  clAllOptions = [];

  // 共享的搜索函数：空查询→加载最新版本，有输入→按versionName搜索
  async function _searchVersions(query, callback) {
    try {
      const params = new URLSearchParams({
        num: '1', trigger: '', type: 'firmware', device,
        client: '', versionCode: '', status: '', build_cause: '', env: '', production: device
      });
      if (query && query.trim().length >= 2) {
        params.set('versionName', query.trim());  // 精确搜索
      }
      // 不加 versionName → 返回最新数据
      const resp = await fetch(`https://open.zepp.top/archive/api/log/notes?${params.toString()}`, { cache: 'no-store' });
      if (!resp.ok) { callback([]); return; }
      const data = await resp.json();
      const detail = (data.detail || []).filter(item => {
        const b = item.branch;
        if (b == null) return false;
        const s = String(b).trim();
        return s !== '' && s.toLowerCase() !== 'null';
      });
      const results = detail.map(item => ({
        value: `${item.versionName}|${item.versionCode}`,
        text: `${item.versionName}  (${item.versionCode})`,
        versionName: item.versionName,
        versionCode: item.versionCode,
        branch: item.branch || '',
        production: item.production || '',
      }));
      results.forEach(opt => { clAllOptions.push(opt); });
      callback(results);
    } catch { callback([]); }
  }

  const searchPlaceholder = '选择版本，或输入前几位搜索';
  const searchRender = {
    option: function(item, escape) {
      return '<div style="white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">' + escape(item.text) + (item.production ? ' <span style="color:#94a3b8;font-size:0.85em">' + escape(item.production) + '</span>' : '') + '</div>';
    },
    no_results: function() { return '<div class="no-results">未找到版本，换个关键词试试</div>'; },
    loading: function() { return '<div class="no-results">加载中...</div>'; }
  };

  // 重建 old 下拉（默认加载列表，输入触发搜索）
  clPrevTomSelect = new TomSelect('#clPrevVersion', {
    valueField: 'value', labelField: 'text', searchField: ['versionName', 'text'],
    placeholder: searchPlaceholder,
    create: false, allowEmptyOption: true, maxOptions: 30,
    load: _searchVersions,
    preload: true,           // 初始化时自动加载
    loadThrottle: 400,       // 输入防抖
    onChange: function(val) { _onClVersionChange('old', val); },
    render: searchRender,
  });

  // 重建 new 下拉
  clCurrTomSelect = new TomSelect('#clCurrVersion', {
    valueField: 'value', labelField: 'text', searchField: ['versionName', 'text'],
    placeholder: searchPlaceholder,
    create: false, allowEmptyOption: true, maxOptions: 30,
    load: _searchVersions,
    preload: true,
    loadThrottle: 400,
    onChange: function(val) { _onClVersionChange('new', val); },
    render: searchRender,
  });

}

function _onClVersionChange(which, val) {
  if (!val) {
    if (which === 'old') {
      clPrevData = null;
      // 清除了旧版本，重建新版本下拉（无过滤）
      _rebuildNewVersionSelect([]);
    } else {
      clCurrData = null;
    }
    _updateClButtonState();
    return;
  }
  const parts = val.split('|');
  const data = { versionName: parts[0] || '', versionCode: parts[1] || '' };

  // 获取该选项的完整数据
  const tom = which === 'old' ? clPrevTomSelect : clCurrTomSelect;
  if (tom) {
    const opt = tom.options[val];
    if (opt) data.branch = opt.branch || '';
  }

  if (which === 'old') {
    clPrevData = data;
    // 重建新版本下拉，内部 load 函数会过滤 versionCode > 旧版本
    const oldCode = parseInt(data.versionCode, 10);
    if (clCurrData && parseInt(clCurrData.versionCode, 10) <= oldCode) {
      clCurrData = null;
    }
    _rebuildNewVersionSelect([]);
  } else {
    // 选择了新版本，校验必须大于旧版本
    if (clPrevData && clPrevData.versionName) {
      const oldCode = parseInt(clPrevData.versionCode, 10);
      const newCode = parseInt(data.versionCode, 10);
      if (newCode <= oldCode) {
        alert('新版本必须大于旧版本，请重新选择！');
        // 清空当前选择
        if (clCurrTomSelect) {
          clCurrTomSelect.clear();
        }
        return;
      }
    }
    clCurrData = data;
    // 自动回填算法 tag
    const clAfterTag = document.getElementById('clAfterTag');
    if (clAfterTag) clAfterTag.value = data.branch || '';
  }
  _updateClButtonState();
}

// 辅助函数：重建新版本下拉框（带旧版本过滤）
function _rebuildNewVersionSelect(options) {
  if (!clCurrTomSelect) return;
  clCurrTomSelect.destroy();
  clCurrTomSelect = new TomSelect('#clCurrVersion', {
    valueField: 'value', labelField: 'text', searchField: ['versionName', 'text'],
    placeholder: '选择版本，或输入前几位搜索',
    create: false, allowEmptyOption: true, maxOptions: 30,
    preload: true,
    loadThrottle: 400,
    load: function(query, callback) {
      const device = selectedDevice;
      const params = new URLSearchParams({
        num: '1', trigger: '', type: 'firmware', device,
        client: '', versionCode: '', status: '', build_cause: '', env: '', production: device
      });
      if (query && query.trim().length >= 2) {
        params.set('versionName', query.trim());
      }
      fetch(`https://open.zepp.top/archive/api/log/notes?${params.toString()}`, { cache: 'no-store' })
        .then(r => r.json())
        .then(data => {
          const detail = (data.detail || []).filter(item => {
        const b = item.branch;
        if (b == null) return false;
        const s = String(b).trim();
        return s !== '' && s.toLowerCase() !== 'null';
      });
          let results = detail.map(item => ({
            value: `${item.versionName}|${item.versionCode}`,
            text: `${item.versionName}  (${item.versionCode})`,
            versionName: item.versionName,
            versionCode: item.versionCode,
            branch: item.branch || '',
            production: item.production || '',
          }));
          // 过滤：只显示 versionCode > 旧版本 versionCode
          if (clPrevData && clPrevData.versionCode) {
            const oldCode = parseInt(clPrevData.versionCode, 10);
            results = results.filter(opt => parseInt(opt.versionCode, 10) > oldCode);
          }
          callback(results);
        })
        .catch(() => callback([]));
    },
    onChange: function(val) { _onClVersionChange('new', val); },
    render: {
      option: function(item, escape) {
        return '<div style="white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">' + escape(item.text) + (item.production ? ' <span style="color:#94a3b8;font-size:0.85em">' + escape(item.production) + '</span>' : '') + '</div>';
      },
      no_results: function() { return '<div class="no-results">未找到更高版本</div>'; },
      loading: function() { return '<div class="no-results">搜索中...</div>'; }
    }
  });
}

function _updateClButtonState() {
  const btn = document.getElementById('btnStartChangelog');
  const hint = document.getElementById('changelogRequiredHint');
  const ok = clPrevData && clPrevData.versionName && clCurrData && clCurrData.versionName;
  if (btn) btn.disabled = !ok;
  if (hint) hint.style.display = ok ? 'none' : '';
}

async function startChangelogBuild() {
  if (!clPrevData || !clCurrData) return;
  const device = selectedDevice;
  if (!device) return;

  const payload = {
    device: device,
    manifest: device.replace(/_\d+m?$/, '') + '.xml',
    prev_version_name: clPrevData.versionName,
    prev_version_code: clPrevData.versionCode,
    curr_version_name: clCurrData.versionName,
    curr_version_code: clCurrData.versionCode,
    tag: clCurrData.branch || '',
    user_id: currentUserId,
    jenkins_auth: getJenkinsAuth(),
  };

  try {
    const resp = await fetch(`/api/projects/${encodeURIComponent(device)}/start-changelog`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const result = await resp.json();
    if (!result.ok) {
      alert(result.error || '启动 changelog 失败');
      return;
    }
    // 关闭弹框
    closeChangelogModal();

    // 跳到第八步
    for (let i = 1; i <= 7; i++) {
      const form = document.getElementById('form' + i);
      if (form) form.style.display = 'none';
    }
    const form8 = document.getElementById('form8');
    if (form8) form8.style.display = 'block';
    updateStepStatus(7, 8);
    // prevStepBefore8 = currentStep;  // [2026-06-02] 注释
    currentStep = 8;
    startTaskListPolling();

    // 连接到 changelog 任务
    switchChangelogTask(result.task_id);
  } catch (err) {
    alert('网络错误: ' + err.message);
  }
}

function startChangelogSSE(taskId) {
  let clReconnectCount = 0;
  const clMaxReconnects = 5;
  if (clEventSource) { clEventSource.close(); clEventSource = null; }
  clReconnectCount = 0;
  const url = `/api/tasks/${encodeURIComponent(taskId)}/stream?user_id=${encodeURIComponent(currentUserId)}`;
  clEventSource = new EventSource(url);
  clEventSource.onmessage = function(event) {
    try {
      const msg = JSON.parse(event.data);
      if (msg.heartbeat) return;

      // 初始状态恢复（重连/切换任务时）
      if (msg.init) {
        console.log('[Changelog SSE] init 消息收到, status:', msg.status, 'feishu_link:', msg.feishu_link, 'logs:', (msg.logs||[]).length, '/', (msg.log_total||0), '行');
        const box = document.getElementById('compileLogBox');
        if (box) box.innerHTML = '';
        logList = [];
        if (msg.logs) {
          msg.logs.forEach(l => {
            addLogAppend(l);
          });
        }
        if (msg.steps) {
          // Changelog 单进度条：用 total_percent 驱动
          if (compileSteps.length === 1 && compileSteps[0].name === 'Changelog 构建') {
            if (msg.total_percent !== undefined) {
              compileSteps[0].percent = msg.total_percent;
              compileSteps[0].status = msg.total_percent >= 100 ? 'success' :
                (['success','failed','timeout','error','terminated'].includes(msg.status) ? msg.status : 'running');
              _stepUIDirty = true;
            }
          } else {
            msg.steps.forEach((s, i) => {
              if (compileSteps[i]) {
                compileSteps[i].status = s.status;
                compileSteps[i].percent = s.percent;
              }
            });
            _stepUIDirty = true;
          }
        }
        if (msg.total_percent !== undefined) {
          totalPercent = msg.total_percent;
        }
        // 恢复飞书差分报告链接或未生成提示（刷新后重连时关键路径）
        if (msg.feishu_link) {
          console.log('[Changelog SSE] 恢复飞书链接:', msg.feishu_link);
          showFeishuLink(msg.feishu_link);
        } else if (msg.feishu_no_link_msg) {
          console.log('[Changelog SSE] 恢复未生成提示:', msg.feishu_no_link_msg);
          showFeishuNoLink(msg.feishu_no_link_msg);
        }
        // 恢复 Jenkins job URL
        if (msg.jenkins_job_url) {
          window._jenkinsJobUrl = msg.jenkins_job_url;
          updateJenkinsBtn();
        }
        renderCompileProgress();
        updateStopButtonVisibility();
        // Changelog 任务 active 但步骤全是 waiting（race condition）：强制显示终止按钮
        if (['running', 'starting', 'triggering'].includes(msg.status)) {
          const runningIdx = compileSteps.findIndex(s => s.status === 'running');
          if (runningIdx === -1) {
            const btnBuild = document.getElementById('btnStopBuild');
            const btnPhase = document.getElementById('btnStopPhase');
            if (btnBuild) btnBuild.classList.remove('hidden');
            if (btnPhase) btnPhase.classList.add('hidden');
          }
        }
        if (msg.status && !['running', 'starting', 'triggering'].includes(msg.status)) {
          clEventSource.close();
          clEventSource = null;
        }
        return;
      }

      // 步骤进度更新
      if (msg.steps) {
        // Changelog 单进度条：用 total_percent 驱动
        if (compileSteps.length === 1 && compileSteps[0].name === 'Changelog 构建') {
          if (msg.total_percent !== undefined) {
            compileSteps[0].percent = msg.total_percent;
            compileSteps[0].status = 'running';
            _stepUIDirty = true;
          }
        } else {
          msg.steps.forEach((s, i) => {
            if (compileSteps[i]) {
              compileSteps[i].status = s.status;
              compileSteps[i].percent = s.percent;
            }
          });
          _stepUIDirty = true;
        }
        if (msg.total_percent !== undefined) {
          totalPercent = msg.total_percent;
        }
        renderCompileProgress();
        updateStopButtonVisibility();
        return;
      }

      // 飞书链接
      if (msg.feishu_link) {
        showFeishuLink(msg.feishu_link);
        return;
      }

      // 未生成差分报告提示
      if (msg.feishu_no_link_msg) {
        showFeishuNoLink(msg.feishu_no_link_msg);
        return;
      }

      // 日志行（统一使用 addLogAppend 终端风格渲染）
      if (msg.line) {
        addLogAppend(msg.line);
      }
      // 完成信号
      if (msg.complete) {
        clEventSource.close();
        clEventSource = null;
        // complete 消息可能包含飞书链接或未生成提示（刷新后重连时）
        if (msg.feishu_link) {
          showFeishuLink(msg.feishu_link);
        } else if (msg.feishu_no_link_msg) {
          showFeishuNoLink(msg.feishu_no_link_msg);
        }
        addLogAppend(msg.status === 'success' ? '🎉 Changelog 生成完成！' : '❌ Changelog 构建失败');
        if (msg.status === 'success') {
          totalPercent = 100;
          compileSteps.forEach(s => { s.status = 'success'; s.percent = 100; });
        } else {
          compileSteps.forEach(s => { if (s.status !== 'success') { s.status = 'failed'; s.percent = 0; } });
        }
        _stepUIDirty = true;
        renderCompileProgress();
        updateStopButtonVisibility();
      }
    } catch (err) { /* ignore */ }
  };
  clEventSource.onerror = function() {
    console.warn('[Changelog SSE] 连接错误, 准备重连...');
    if (clEventSource) { clEventSource.close(); clEventSource = null; }
    clReconnectCount++;
    if (clReconnectCount > clMaxReconnects) {
      console.warn('[Changelog SSE] 已达最大重连次数, 停止重连');
      return;
    }
    if (clActiveTaskId) {
      setTimeout(() => {
        if (clActiveTaskId && !clEventSource) {
          console.log('[Changelog SSE] 自动重连...');
          startChangelogSSE(clActiveTaskId);
        }
      }, 3000);
    }
  };
}

/** 显示飞书差分报告链接（成功态：绿色边框 + 链接） */
function showFeishuLink(link) {
  const card = document.getElementById('feishuLinkCard');
  const icon = document.getElementById('feishuLinkIcon');
  const title = document.getElementById('feishuLinkTitle');
  const urlEl = document.getElementById('feishuLinkUrl');
  const textEl = document.getElementById('feishuLinkText');
  const noLinkMsg = document.getElementById('feishuNoLinkMsg');
  if (!card) return;

  // 成功态：绿色边框 + ✅ + 链接
  card.className = 'mb-4 rounded-lg border-2 border-green-400 bg-green-50 p-4 transition-all duration-300';
  if (icon) { icon.className = 'fa fa-check-circle text-green-500 text-lg'; }
  if (title) { title.className = 'font-semibold text-green-700 text-sm'; title.textContent = '差分报告已生成'; }
  if (urlEl) { urlEl.href = link; urlEl.className = 'inline-flex items-center gap-2 text-blue-600 hover:text-blue-800 hover:underline font-mono text-sm break-all'; }
  if (textEl) { textEl.textContent = link; }
  if (noLinkMsg) { noLinkMsg.className = 'hidden text-sm'; }
  card.classList.remove('hidden');
}

/** 显示未生成差分报告提示（警告态：黄色边框 + 提示文字） */
function showFeishuNoLink(msg) {
  const card = document.getElementById('feishuLinkCard');
  const icon = document.getElementById('feishuLinkIcon');
  const title = document.getElementById('feishuLinkTitle');
  const urlEl = document.getElementById('feishuLinkUrl');
  const noLinkMsg = document.getElementById('feishuNoLinkMsg');
  if (!card) return;

  // 警告态：黄色边框 + ⚠️ + 提示
  card.className = 'mb-4 rounded-lg border-2 border-yellow-400 bg-yellow-50 p-4 transition-all duration-300';
  if (icon) { icon.className = 'fa fa-exclamation-triangle text-yellow-500 text-lg'; }
  if (title) { title.className = 'font-semibold text-yellow-700 text-sm'; title.textContent = '差分报告未生成'; }
  if (urlEl) { urlEl.className = 'hidden'; }
  if (noLinkMsg) { noLinkMsg.className = 'text-sm text-yellow-600'; noLinkMsg.textContent = msg; }
  card.classList.remove('hidden');
}

// ================== 差分版本历史下拉 ==================
let diffTomSelect = null;

function formatDate(raw) {
  if (!raw) return '';
  // raw format: 20260522_17_38_05  or  20260522
  const d = raw.replace(/_/g, '').substring(0, 8);
  if (d.length < 8) return raw;
  return d.substring(0, 4) + '/' + d.substring(4, 6) + '/' + d.substring(6, 8);
}

// 比较两个版本号字符串（x.y.z.w），返回负数/0/正数
function _compareVersionNames(a, b) {
  const pa = String(a).split('.').map(Number);
  const pb = String(b).split('.').map(Number);
  const len = Math.max(pa.length, pb.length);
  for (let i = 0; i < len; i++) {
    const va = pa[i] || 0;
    const vb = pb[i] || 0;
    if (va !== vb) return va - vb;
  }
  return 0;
}

async function fetchDiffVersions(device) {
  const selectEl = document.getElementById('diffVersionSelect');
  if (!selectEl || !device) return;

  if (diffTomSelect) {
    diffTomSelect.destroy();
    diffTomSelect = null;
  }

  // 先显示加载中
  diffTomSelect = new TomSelect('#diffVersionSelect', {
    options: [{ value: '', text: '加载中...' }],
    placeholder: '正在加载差分版本列表...',
    create: false,
    allowEmptyOption: true,
    maxOptions: null,
  });

  try {
    const params = new URLSearchParams({
      num: '1',
      trigger: '',
      type: 'firmware',
      device: device,
      client: '',
      versionCode: '',
      versionName: '',
      status: '',
      build_cause: '',
      env: '',
      production: device,
    });
    const resp = await fetch(`https://open.zepp.top/archive/api/log/notes?${params.toString()}`, { cache: 'no-store' });
    if (!resp.ok) throw new Error(`请求失败：${resp.status}`);
    const data = await resp.json();
    const detail = (data.detail || []).filter(item => item.branch != null && String(item.branch).trim());

    if (!detail.length) {
      diffTomSelect.destroy();
      diffTomSelect = new TomSelect('#diffVersionSelect', {
        options: [],
        placeholder: '暂无差分版本数据',
        create: false,
        allowEmptyOption: true,
        maxOptions: null,
        render: {
          no_results: function() { return '<div class="no-results">未找到差分版本</div>'; }
        }
      });
      return;
    }

    const options = detail.map(item => ({
      value: `${item.versionName}|${item.versionCode}`,
      text: item.versionName,
      versionCode: item.versionCode,
      trigger: item.trigger || '',
      updatedParsed: formatDate(item.updatedParsed),
      production: item.production || '',
    }));

    // 按 versionCode 去重，相同 versionCode 只保留 versionName 最低的那条
    const deduped = [];
    const seen = {};
    for (const opt of options) {
      const code = opt.versionCode;
      if (!seen.hasOwnProperty(code)) {
        seen[code] = opt;
        deduped.push(opt);
      } else {
        // 比较 versionName，保留更低版本
        const existing = seen[code];
        if (_compareVersionNames(opt.text, existing.text) < 0) {
          // 替换旧数据
          const idx = deduped.indexOf(existing);
          if (idx >= 0) {
            deduped[idx] = opt;
            seen[code] = opt;
          }
        }
      }
    }

    diffTomSelect.destroy();
    diffTomSelect = new TomSelect('#diffVersionSelect', {
      options: deduped,
      placeholder: '请选择差分目标版本...',
      maxOptions: null,
      create: false,
      sortField: { field: 'updatedParsed', direction: 'desc' },
      highlight: true,
      allowEmptyOption: true,
      render: {
        option: function(item, escape) {
          return `<div class="diff-option">
            <div class="diff-option-main">
              <span class="diff-version-name">${escape(item.text || '')}</span>
              <span class="diff-version-code">${escape(item.versionCode || '')}</span>
            </div>
            <div class="diff-option-sub">
              <span>${escape(item.production || '')}</span>
              <span style="margin-left:0.75rem;">${escape(item.trigger || '')}</span>
              <span style="margin-left:0.75rem;">${escape(item.updatedParsed || '')}</span>
            </div>
          </div>`;
        },
        item: function(item, escape) {
          return `<div>${escape(item.text || '')}&nbsp;<span style="color:#94a3b8;font-size:0.85em">${escape(item.versionCode || '')}</span></div>`;
        },
        no_results: function() {
          return '<div class="no-results">未找到差分版本</div>';
        }
      },
      onChange: function(value) {
        if (!value) {
          document.getElementById('diff_name').value = '';
          document.getElementById('diff_code').value = '';
          return;
        }
        const parts = value.split('|');
        const diffNameEl = document.getElementById('diff_name');
        const diffCodeEl = document.getElementById('diff_code');
        if (diffNameEl) { diffNameEl.value = parts[0] || ''; diffNameEl.dispatchEvent(new Event('input')); }
        if (diffCodeEl) { diffCodeEl.value = parts[1] || ''; diffCodeEl.dispatchEvent(new Event('input')); }
      }
    });

    // 如果 diff_name/diff_code 已有值（历史版本回填），同步选中
    const existingName = document.getElementById('diff_name')?.value.trim();
    const existingCode = document.getElementById('diff_code')?.value.trim();
    if (existingName && existingCode) {
      diffTomSelect.setValue(`${existingName}|${existingCode}`);
    }
  } catch (err) {
    console.error('获取差分版本列表失败:', err);
    if (diffTomSelect) { diffTomSelect.destroy(); diffTomSelect = null; }
    diffTomSelect = new TomSelect('#diffVersionSelect', {
      options: [],
      placeholder: '差分版本加载失败，请手动输入',
      create: true,
      allowEmptyOption: true,
      maxOptions: null,
    });
  }
}

async function fetchHistoryVersions(device) {
  const versionList = document.getElementById('versionList');
  if (!versionList) return;
  versionList.innerHTML = '<div class="text-center text-gray-400 py-8">加载中...</div>';

  try {
    const resp = await fetch(`/api/projects/${encodeURIComponent(device)}/history`, { cache: 'no-store' });
    if (!resp.ok) throw new Error(`请求失败：${resp.status}`);
    const data = await resp.json();
    const history = (data.history || []).slice(0, 5);

    if (!history.length) {
      versionList.innerHTML = '<div class="text-center text-gray-400 py-8">暂无历史版本</div>';
      return;
    }

    versionList.innerHTML = '';
    history.forEach(item => {
      const ts = item.timestamp || '';
      const formattedTs = ts.length >= 8 ? ts.substring(2,4) + '/' + ts.substring(4,6) + '/' + ts.substring(6,8) : ts;
      const variant = item.variant || `${item.project || device} ${item.stage || ''} ${item.version || ''}`.trim();
      const notes = item.notes || '';
      const tag = item.tag || '';
      const bootTag = item.boot_tag || '';
      const recoveryTag = item.recovery_tag || '';
      const fctTag = item.fct_tag || '';

      const versionData = {
        projectId: item.project || device,
        stage: item.stage || '',
        version: item.version || '',
        branch: notes,
        docName: variant,
        buildParams: {
          tag_algo: tag,
          tag_boot: bootTag,
          tag_recovery: recoveryTag,
          tag_fct: fctTag,
          ver_release: '',
          ver_debug: '',
          ver_fct: '',
          diff_name: '',
          diff_code: '',
          build_content: '',
          fw_ver_strategy_env: 'none',
          build_tscan: 'no',
          auto_bind_after_upgrade: 'no'
        }
      };
      const div = document.createElement('div');
      div.className = 'version-item';
      div._versionData = versionData;
      div.setAttribute('onclick', `selectVersion(this, '${item.project || device}', '${ts}')`);
      div.innerHTML = `
        <div class="font-medium text-gray-800">${variant} <span class="text-sm text-gray-400 font-normal">— ${formattedTs}</span></div>
        <div class="text-sm text-gray-500 mt-1"><strong>分支：</strong>${notes || '--'}</div>
        <div class="text-sm text-gray-500 mt-1"><strong>算法tag：</strong>${tag || '--'} &nbsp; <strong>Boot_Tag：</strong>${bootTag || '--'}</div>
        <div class="text-sm text-gray-500 mt-1"><strong>Recovery_Tag：</strong>${recoveryTag || '--'} &nbsp; <strong>Fct_Tag：</strong>${fctTag || '--'}</div>
      `;
      versionList.appendChild(div);
    });
  } catch (err) {
    console.error('fetchHistoryVersions error:', err);
    versionList.innerHTML = '<div class="text-center text-gray-400 py-8">加载失败，请重试</div>';
  }
}

function clearAllInput(){
  const inputIds = ['projectId','projectStage','versionNumber','publishBranch','docName','wiki_token','tag_algo','tag_boot','tag_recovery','tag_fct','ver_release','ver_debug','diff_name','diff_code','build_content'];
  inputIds.forEach(id=>{ const el = document.getElementById(id); if (el) el.value = ''; });
  const fwVerEnvEl = document.getElementById('fw_ver_strategy_env'); if (fwVerEnvEl) fwVerEnvEl.value = 'none';
  const tscanEl = document.getElementById('build_tscan'); if (tscanEl) tscanEl.checked = false;
  const autoBindEl = document.getElementById('auto_bind_after_upgrade'); if (autoBindEl) autoBindEl.checked = false;
  initBuildContentCheckboxes(''); resetEditState(); selectedVersionData = null; canGoNextStep4 = false;
  if (diffTomSelect) { diffTomSelect.clear(); }
  if (branchTomSelect) { branchTomSelect.clear(); }
  if (wikiTokenTomSelect) { wikiTokenTomSelect.clear(); }  // TomSelect 管理的字段需要 API 清空
  const toForm5Btn = document.getElementById('toForm5Btn'); if (toForm5Btn) { toForm5Btn.classList.add('btn-disabled'); toForm5Btn.classList.remove('btn-primary'); }
}

function checkVersionFormat(){
  const vEl = document.getElementById('versionNumber'); const errorTipEl = document.getElementById('versionErrorTip');
  if (!vEl || !errorTipEl) return; const v = vEl.value.trim(); errorTipEl.classList.toggle('show', v && !versionRegex.test(v));
}

// ================== 历史版本详情填充 ==================
function fillHistoryVersionDetail(prefix) {
  const wrap = document.getElementById(`${prefix}HistDetailWrap`);
  if (publishType !== 'oldProject' || !selectedVersionData) {
    if (wrap) wrap.classList.add('hidden');
    return;
  }
  const b = selectedVersionData.buildParams || {};
  const items = [
    { id: `${prefix}HistVariant`, value: selectedVersionData.docName || '--' },
    { id: `${prefix}HistBranch`, value: selectedVersionData.branch || '--' },
    { id: `${prefix}HistTagAlgo`, value: b.tag_algo || '--' },
    { id: `${prefix}HistTagBoot`, value: b.tag_boot || '--' },
    { id: `${prefix}HistTagRecovery`, value: b.tag_recovery || '--' },
    { id: `${prefix}HistTagFct`, value: b.tag_fct || '--' },
  ];
  items.forEach(item => {
    const el = document.getElementById(item.id);
    if (el) el.textContent = item.value;
  });
  if (wrap) wrap.classList.remove('hidden');
}

// ================== 跳过 Jenkins 编译 → 直接跑 Pipeline ==================
async function runDirectPipeline() {
  // 验证 Jenkins 登录
  if (!getJenkinsAuth()) {
    showLoginModal();
    document.getElementById('loginCancelBtn').onclick = function() { hideLoginModal(); };
    alert('请先登录 Jenkins 账号再提交任务');
    return;
  }

  const project = document.getElementById('projectId')?.value.trim() || '';
  const stage = document.getElementById('projectStage')?.value.trim() || '';
  const version = document.getElementById('versionNumber')?.value.trim() || '';
  const notes = getBranchValue();

  if (!project) { alert('请填写项目标识'); return; }
  if (!stage) { alert('请填写项目阶段'); return; }
  if (!version || !versionRegex.test(version)) { alert('版本号格式不正确'); return; }

  const releaseUrl = document.getElementById('releaseJenkinsUrl')?.value.trim() || '';
  const debugUrl = document.getElementById('debugJenkinsUrl')?.value.trim() || '';
  if (!releaseUrl) { alert('请填写 Release Jenkins 链接'); return; }
  if (!debugUrl) { alert('请填写 Debug Jenkins 链接'); return; }

  // 收集构建参数（与 nextStep7 一致）
  const tag_algo = document.getElementById('tag_algo')?.value.trim() || '';
  const tag_boot = document.getElementById('tag_boot')?.value.trim() || '';
  const tag_recovery = document.getElementById('tag_recovery')?.value.trim() || '';
  const tag_fct = document.getElementById('tag_fct')?.value.trim() || '';
  const ver_release = document.getElementById('ver_release')?.value.trim() || '';
  const ver_debug = document.getElementById('ver_debug')?.value.trim() || '';
  const ver_fct = document.getElementById('ver_fct')?.value.trim() || '';
  const diff_name = document.getElementById('diff_name')?.value.trim() || '';
  const diff_code = document.getElementById('diff_code')?.value.trim() || '';
  const build_content = document.getElementById('build_content')?.value.trim() || '';
  const fw_ver_strategy_env = document.getElementById('fw_ver_strategy_env')?.value || 'none';
  const build_tscan = document.getElementById('build_tscan')?.checked ? 'yes' : 'no';
  const auto_bind_after_upgrade = document.getElementById('auto_bind_after_upgrade')?.checked ? 'yes' : 'no';
  const wiki_token = document.getElementById('wiki_token')?.value.trim() || '';

  const variant = `${project} ${stage} v${version}`.trim().replace(/\s+v$/g, '');

  const payload = {
    release: { project, device_name: project, stage, version, notes, variant },
    vars: {
      tag: tag_algo, boot_tag: tag_boot, recovery_tag: tag_recovery, fct_tag: tag_fct,
      release_version_name: ver_release, debug_version_name: ver_debug, fct_version_name: ver_fct,
      prev_version_name: diff_name, prev_version_code: diff_code,
      build_mode: build_content, fw_ver_strategy_env, build_tscan, auto_bind_after_upgrade,
    },
    feishu: { template_node_token: wiki_token },
    release_jenkins_url: releaseUrl,
    debug_jenkins_url: debugUrl,
    jenkins_auth: getJenkinsAuth(),
    user_id: currentUserId,
    skip_jenkins_trigger: true,
    skip_flags: {
      skip_download: document.getElementById('skipDownload')?.checked || false,
      skip_prepare: document.getElementById('skipPrepare')?.checked || false,
      skip_upload: document.getElementById('skipUpload')?.checked || false,
      skip_share: document.getElementById('skipShare')?.checked || false,
      skip_doc: document.getElementById('skipDoc')?.checked || false,
      skip_feishu: document.getElementById('skipFeishu')?.checked || false,
    },
  };

  let taskId = '';
  try {
    const resp = await fetch(`/api/projects/${encodeURIComponent(project)}/run-pipeline-direct`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const result = await resp.json();
    if (!result.ok) {
      alert('启动失败：' + (result.error || '未知错误'));
      return;
    }
    taskId = result.task_id;
  } catch (err) {
    alert('请求失败：' + err.message);
    return;
  }

  // 跳到第八步
  // prevStepBefore8 = currentStep;  // [2026-06-02] 注释
  toggleForm(5, 8); updateStepStatus(5, 8); currentStep = 8;
  startCompileTask(taskId);
}

// ================== 第七步 → 第八步：触发编译 ==================
async function nextStep7() {
  // 检查是否已登录 Jenkins
  if (!getJenkinsAuth()) {
    showLoginModal();
    document.getElementById('loginCancelBtn').onclick = function() { hideLoginModal(); };
    alert('请先登录 Jenkins 账号再提交任务');
    return;
  }
  // 收集第七步确认页面的所有数据
  const project = document.getElementById('projectId')?.value.trim() || '';
  const stage = document.getElementById('projectStage')?.value.trim() || '';
  const version = document.getElementById('versionNumber')?.value.trim() || '';
  const notes = getBranchValue();
  const variant = document.getElementById('docName')?.value.trim() || '';
  const tag_algo = document.getElementById('tag_algo')?.value.trim() || '';
  const tag_boot = document.getElementById('tag_boot')?.value.trim() || '';
  const tag_recovery = document.getElementById('tag_recovery')?.value.trim() || '';
  const tag_fct = document.getElementById('tag_fct')?.value.trim() || '';
  const ver_release = document.getElementById('ver_release')?.value.trim() || '';
  const ver_debug = document.getElementById('ver_debug')?.value.trim() || '';
  const ver_fct = ver_release;  // fct 版本号始终等于 release 版本号
  const diff_name = document.getElementById('diff_name')?.value.trim() || '';
  const diff_code = document.getElementById('diff_code')?.value.trim() || '';
  const build_content = document.getElementById('build_content')?.value.trim() || '';
  const fw_ver_strategy_env = document.getElementById('fw_ver_strategy_env')?.value || 'none';
  const build_tscan = document.getElementById('build_tscan')?.checked ? 'yes' : 'no';
  const auto_bind_after_upgrade = document.getElementById('auto_bind_after_upgrade')?.checked ? 'yes' : 'no';
  const hmi_core_mm_owner_dep = document.getElementById('hmi_core_mm_owner_dep')?.value.trim() || '';
  const wiki_token = document.getElementById('wiki_token')?.value.trim() || '';

  // 构造请求体，匹配后端 /api/projects/<project>/release 接口
  const payload = {
    release: {
      project: project,
      device_name: project,
      stage: stage,
      version: version,
      notes: notes,
      variant: variant,
    },
    vars: {
      tag: tag_algo,
      boot_tag: tag_boot,
      recovery_tag: tag_recovery,
      fct_tag: tag_fct,
      release_version_name: ver_release,
      debug_version_name: ver_debug,
      fct_version_name: ver_fct,
      prev_version_name: diff_name,
      prev_version_code: diff_code,
      build_mode: build_content,
      fw_ver_strategy_env: fw_ver_strategy_env,
      build_tscan: build_tscan,
      auto_bind_after_upgrade: auto_bind_after_upgrade,
      hmi_core_mm_owner_dep: hmi_core_mm_owner_dep,
    },
    feishu: {
      template_node_token: wiki_token,
    },
    jenkins_job_url: (platformJenkinsJobUrl && platformJenkinsJobUrl[selectedPlatform]) || '',
    platform_select: (platformSelectValue && platformSelectValue[selectedPlatform]) || '',
    jenkins_auth: getJenkinsAuth(),
    user_id: currentUserId,
    notification_open_id: getNotifyOpenId(),
    is_new: publishType === 'newProject',
  };

  let taskId = '';
  try {
    const resp = await fetch(`/api/projects/${encodeURIComponent(project)}/release`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const result = await resp.json();
    if (!result.ok) {
      alert('保存配置失败：' + (result.error || '未知错误'));
      return;
    }
    taskId = result.task_id;
    console.log('配置已保存到 JSON 文件:', result.config_file, '任务ID:', taskId);
  } catch (err) {
    alert('保存配置时发生网络错误：' + err.message);
    return;
  }

  // 保存成功后，进入第八步开始编译（传入 task_id 以轮询后端实时日志）
  startCompileTask(taskId);
}

// ================== 第八步：编译任务执行（轮询后端实时日志）==================
// ================== 第八步：任务列表管理 ==================
let activeTaskId = null;
let taskListPollTimer = null;
let tasksCache = {};  // task_id → task 数据的缓存

function refreshTaskList() {
  fetch('/api/tasks?user_id=' + encodeURIComponent(currentUserId))
    .then(r => r.json())
    .then(data => {
      if (!data.ok) return;
      // 更新缓存
      tasksCache = {};
      (data.tasks || []).forEach(t => { tasksCache[t.task_id] = t; });
      renderTaskSidebar(data.tasks || []);
      updateRunningTaskBtn(data.tasks || []);
    })
    .catch(err => { console.error('refreshTaskList failed:', err); });
}

function renderTaskSidebar(tasks) {
  const sidebar = document.getElementById('taskListSidebar');
  if (!sidebar) return;
  if (!tasks.length) {
    sidebar.innerHTML = '<div class="text-xs text-gray-400 text-center py-4">暂无任务</div>';
    return;
  }
  const newHtml = tasks.map(t => {
    const isChangelog = t.task_type === 'changelog';
    const cls = (t.task_id === activeTaskId || t.task_id === clActiveTaskId) ? 'task-item active' : 'task-item';
    const sc = t.status === 'running' || t.status === 'triggering' || t.status === 'starting' ? 'task-status-running' :
               t.status === 'success' ? 'task-status-success' :
               t.status === 'failed' || t.status === 'error' || t.status === 'terminated' || t.status === 'timeout' ? 'task-status-failed' : 'task-status-running';
    const time = (t.started_at || '').substring(11, 19) || '';
    // 所有任务都显示 × 按钮（运行中的任务点击时会先终止 Jenkins 构建再移除）
    const isRunning = t.status === 'running' || t.status === 'starting';
    const doneIcon = t.status === 'success' ? '<i class="fa fa-check-circle" style="color:#22c55e;margin-left:4px;"></i>' : '';
    const removeBtn = `<button class="task-remove-btn${isRunning ? ' task-remove-running' : ''}" onclick="event.stopPropagation();removeTask('${t.task_id}', ${isRunning})" title="${isRunning ? '移除任务（将同时终止 Jenkins 构建）' : '移除任务'}"><i class="fa fa-times"></i></button>`;
    const isTscan = t.task_type === 'tscan';
    const isSkipJenkins = t.task_type === 'skip_jenkins';
    const badgeStyle = 'font-size:0.65rem;color:#fff;padding:0 4px;border-radius:3px;margin-left:4px;';
    let taskLabel = '';
    if (isChangelog) {
      taskLabel = `<span class="task-type-badge" style="${badgeStyle}background:#8b5cf6;">changelog</span>`;
    } else if (isTscan) {
      taskLabel = `<span class="task-type-badge" style="${badgeStyle}background:#22c55e;">build tscan</span>`;
    } else if (isSkipJenkins) {
      taskLabel = `<span class="task-type-badge" style="${badgeStyle}background:#f59e0b;">skip jenkins to docs</span>`;
    } else {
      taskLabel = `<span class="task-type-badge" style="${badgeStyle}background:#3b82f6;">build version</span>`;
    }
    const clickHandler = isChangelog ? `switchChangelogTask('${t.task_id}')` : (isTscan ? `switchTscanTask('${t.task_id}')` : `switchTask('${t.task_id}')`);
    return `<div class="${cls}" onclick="${clickHandler}">
      <div class="flex items-center">
        <span class="task-item-status ${sc}"></span>
        <div class="flex-1 min-w-0">
          <div class="task-item-name">${escapeHtml(t.project)}${taskLabel}${doneIcon}</div>
          <div class="task-item-time">${t.version ? 'v' + escapeHtml(t.version) + ' · ' : ''}${time} · ${t.status}${t.total_percent ? ' · ' + t.total_percent + '%' : ''}</div>
        </div>
        ${removeBtn}
      </div>
    </div>`;
  }).join('');
  // 只有内容真正变化才更新 DOM，避免无意义的闪烁
  if (sidebar.innerHTML !== newHtml) {
    sidebar.innerHTML = newHtml;
  }
}

function switchChangelogTask(taskId) {
  if (clActiveTaskId === taskId) return;
  stopCurrentPolling();  // 停止编译任务的 SSE 连接
  clActiveTaskId = taskId;
  activeTaskId = taskId;
  window._taskType = '';
  window._taskPlatform = '';
  // 从缓存读取任务类型和平台 → 设置 Jenkins 按钮
  const t = tasksCache[taskId];
  if (t) {
    window._taskType = t.task_type || '';
    window._taskPlatform = t.platform_select || '';
  }
  updateJenkinsBtn();
  localStorage.setItem('activeTaskId', taskId);
  // 初始化 Changelog 专用步骤
  compileSteps = [
    { name: 'Changelog 构建', status: 'waiting', percent: 0 },
  ];
  totalPercent = 0;
  _stepUIDirty = true;
  document.getElementById('currentTaskTitle').textContent = 'Changelog: ' + taskId;
  document.getElementById('currentTaskTitle').style.display = '';
  // 隐藏跨任务卡片
  [document.getElementById('feishuLinkCard'),
   document.getElementById('tscanProgressSection'),
   document.getElementById('tscanResultCard')].forEach(el => { if (el) el.classList.add('hidden'); });
  const logBox = document.getElementById('compileLogBox');
  if (logBox) logBox.innerHTML = '';
  const logCard = logBox ? logBox.closest('.build-card') : null;
  if (logCard) {
    const title = logCard.querySelector('.build-card-title');
    if (title) title.textContent = 'Changelog 实时日志';
  }
  renderCompileProgress();
  hideAllStopButtons();
  startChangelogSSE(taskId);
  refreshTaskList();
}

function switchTscanTask(taskId) {
  if (activeTaskId === taskId && window._isTscanStandalone) return;
  stopCurrentPolling();
  if (clEventSource) { clEventSource.close(); clEventSource = null; }
  clActiveTaskId = null;
  activeTaskId = taskId;
  window._taskType = '';
  window._taskPlatform = '';
  // 从缓存读取任务类型和平台 → 设置 Jenkins 按钮
  const t = tasksCache[taskId];
  if (t) {
    window._taskType = t.task_type || '';
    window._taskPlatform = t.platform_select || '';
  }
  updateJenkinsBtn();
  window._isTscanStandalone = true;
  localStorage.setItem('activeTaskId', taskId);
  compileSteps = [
    { name: 'TSCAN构建', status: 'waiting', percent: 0 },
  ];
  totalPercent = 0;
  _stepUIDirty = true;
  document.getElementById('currentTaskTitle').textContent = 'TSCAN: ' + taskId;
  document.getElementById('currentTaskTitle').style.display = '';
  // 隐藏跨任务卡片
  [document.getElementById('feishuLinkCard'),
   document.getElementById('tscanProgressSection'),
   document.getElementById('tscanResultCard')].forEach(el => { if (el) el.classList.add('hidden'); });
  const logBox = document.getElementById('compileLogBox');
  if (logBox) logBox.innerHTML = '';
  const logCard = logBox ? logBox.closest('.build-card') : null;
  if (logCard) {
    const title = logCard.querySelector('.build-card-title');
    if (title) title.textContent = 'TSCAN 实时日志';
  }
  renderCompileProgress();
  hideAllStopButtons();
  startSSEStream(taskId);
  refreshTaskList();
}

async function removeTask(taskId, isRunning) {
  const warnMsg = isRunning
    ? '⚠️ 该任务正在运行中，移除将同时终止 Jenkins 上的远程构建，确定要继续吗？'
    : '确定要移除此任务吗？';
  if (!confirm(warnMsg)) return;
  async function doRemove(force) {
    const url = `/api/tasks/${encodeURIComponent(taskId)}/remove` + (force ? '?force=true' : '');
    const body = { user_id: currentUserId };
    if (force) body.force = true;
    const resp = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await resp.json();
    return data;
  }
  try {
    // running 任务直接用 force=true（已在上方 confirm 中提醒用户会终止 Jenkins）
    let data = await doRemove(isRunning);
    if (!data.ok) {
      alert(data.error || '移除失败');
      return;
    }
    // 如果移除的是当前正在查看的任务，清空进度条和日志
    if (activeTaskId === taskId) {
      activeTaskId = null;
      localStorage.removeItem('activeTaskId');
      stopCurrentPolling();
      // 重置进度条
      compileSteps.forEach(s => { s.status = "waiting"; s.percent = 0; });
      totalPercent = 0;
      renderCompileProgress();
      // 清空日志
      logList = [];
      const box = document.getElementById('compileLogBox');
      if (box) box.innerHTML = '';
      document.getElementById('currentTaskTitle').textContent = '任务: --';
      updateStopButtonVisibility();
      // 隐藏 TSCAN 子任务进度
      const tscanSection = document.getElementById('tscanProgressSection');
      if (tscanSection) tscanSection.classList.add('hidden');
    }
    if (clActiveTaskId === taskId) {
      clActiveTaskId = null;
      if (clEventSource) { clEventSource.close(); clEventSource = null; }
      const logBox = document.getElementById('compileLogBox');
      if (logBox) logBox.innerHTML = '';
      document.getElementById('currentTaskTitle').textContent = '任务: --';
      // 同步清理 localStorage（switchChangelogTask 现在也写入 activeTaskId）
      if (activeTaskId === taskId) {
        activeTaskId = null;
        localStorage.removeItem('activeTaskId');
      }
      // 隐藏飞书链接卡片
      const flCard = document.getElementById('feishuLinkCard');
      if (flCard) flCard.classList.add('hidden');
    }
    refreshTaskList();
  } catch (err) {
    alert('网络错误: ' + err.message);
  }
}

function switchTask(taskId) {
  if (activeTaskId === taskId) return;
  stopCurrentPolling();
  // 同时停止 changelog SSE 连接并清除 changelog active 状态
  if (clEventSource) { clEventSource.close(); clEventSource = null; }
  clActiveTaskId = null;
  window._isTscanStandalone = false;
  window._taskType = '';
  window._taskPlatform = '';
  // 从缓存读取任务类型和平台 → 设置 Jenkins 按钮
  const t = tasksCache[taskId];
  if (t) {
    window._taskType = t.task_type || '';
    window._taskPlatform = t.platform_select || '';
  }
  updateJenkinsBtn();
  // 恢复 btnStopBuild 默认文字
  const btnBuildReset = document.getElementById('btnStopBuild');
  if (btnBuildReset) btnBuildReset.innerHTML = '<i class="fa fa-stop"></i> 终止版本编译';
  // 清空跨任务卡片
  [document.getElementById('feishuLinkCard'),
   document.getElementById('tscanProgressSection'),
   document.getElementById('tscanResultCard')].forEach(el => { if (el) el.classList.add('hidden'); });
  activeTaskId = taskId;
  localStorage.setItem('activeTaskId', taskId);
  // 恢复正常构建步骤
  compileSteps = [
    { name: "版本编译", status: "waiting", percent: 0 },
    { name: "本地下载", status: "waiting", percent: 0 },
    { name: "NAS文件上传", status: "waiting", percent: 0 },
    { name: "分享链接", status: "waiting", percent: 0 },
    { name: "生成飞书文档", status: "waiting", percent: 0 },
  ];
  totalPercent = 0;
  _stepUIDirty = true;
  // 隐藏飞书链接卡片
  const flCard = document.getElementById('feishuLinkCard');
  if (flCard) flCard.classList.add('hidden');

  // 不重置显示——SSE 连接时后端会推送完整历史日志和步骤状态
  document.getElementById('currentTaskTitle').textContent = '任务: ' + taskId;
  renderCompileProgress();
  refreshTaskList();
  startSSEStream(taskId);
}

function updateRunningTaskBtn(tasks) {
  const btn = document.getElementById('runningTaskBtn');
  const countEl = document.getElementById('runningTaskCount');
  if (!btn) return;
  // 以任务列表数量控制显示，只要有任务就显示（包括已完成的）
  if (tasks.length > 0) {
    btn.classList.remove('hidden');
    if (countEl) countEl.textContent = tasks.length;
  } else {
    btn.classList.add('hidden');
  }
}

function gotoRunningTask() {
  fetch('/api/tasks?user_id=' + encodeURIComponent(currentUserId))
    .then(r => r.json())
    .then(data => {
      if (!data.ok || !data.tasks.length) return;
      const running = data.tasks.filter(t => t.status === 'running' || t.status === 'triggering' || t.status === 'starting');
      const first = running[0] || data.tasks[0];
      if (first) {
        // 隐藏所有步骤，直接跳到第八步
        for (let i = 1; i <= 7; i++) {
          const form = document.getElementById('form' + i);
          if (form) form.style.display = 'none';
        }
        const form8 = document.getElementById('form8');
        if (form8) form8.style.display = 'block';
        updateStepStatus(7, 8);
        // prevStepBefore8 = currentStep;  // [2026-06-02] 注释
        currentStep = 8;
        if (first.task_type === 'changelog') {
          switchChangelogTask(first.task_id);
        } else if (first.task_type === 'tscan') {
          switchTscanTask(first.task_id);
        } else {
          switchTask(first.task_id);
        }
      }
    });
}

// ================== 浮动按钮拖拽 + 位置记忆 ==================
function initDragRunningTaskBtn() {
  const LS_KEY = 'runningTaskBtnPos';
  const DRAG_THRESHOLD = 5;  // 移动超过 5px 视为拖拽

  const btn = document.getElementById('runningTaskBtn');
  if (!btn) return;

  // 恢复上次保存的位置
  try {
    const saved = JSON.parse(localStorage.getItem(LS_KEY));
    if (saved && typeof saved.top === 'number' && typeof saved.right === 'number') {
      btn.style.top = Math.max(0, Math.min(window.innerHeight - 60, saved.top)) + 'px';
      btn.style.right = Math.max(0, Math.min(window.innerWidth - 60, saved.right)) + 'px';
    }
  } catch (e) { /* ignore */ }

  let dragging = false;
  let startX, startY, startTop, startRight;
  let moved = false;

  btn.addEventListener('mousedown', function(e) {
    if (e.button !== 0) return;  // 只响应左键
    dragging = true;
    moved = false;
    startX = e.clientX;
    startY = e.clientY;
    startTop = parseInt(getComputedStyle(btn).top) || 0;
    startRight = parseInt(getComputedStyle(btn).right) || 0;
    // 不在这里 preventDefault，否则会阻止 click 事件
  });

  document.addEventListener('mousemove', function(e) {
    if (!dragging) return;
    const dx = startX - e.clientX;
    const dy = e.clientY - startY;
    const dist = Math.sqrt(dx * dx + dy * dy);
    if (dist > DRAG_THRESHOLD) {
      if (!moved) {
        moved = true;
        btn.classList.add('dragging');
      }
      e.preventDefault();  // 仅在真正拖拽时阻止文字选择
      btn.style.top = Math.max(0, Math.min(window.innerHeight - 60, startTop + dy)) + 'px';
      btn.style.right = Math.max(0, Math.min(window.innerWidth - 60, startRight + dx)) + 'px';
    }
  });

  document.addEventListener('mouseup', function(e) {
    if (!dragging) return;
    dragging = false;
    btn.classList.remove('dragging');

    if (moved) {
      // 保存拖拽后的位置
      try {
        localStorage.setItem(LS_KEY, JSON.stringify({
          top: parseInt(btn.style.top) || parseInt(getComputedStyle(btn).top),
          right: parseInt(btn.style.right) || parseInt(getComputedStyle(btn).right),
        }));
      } catch (ex) { /* ignore */ }
    } else {
      // 未拖拽 → 视为点击
      gotoRunningTask();
    }
  });
}

// 页面加载时检测进行中的任务
function checkRunningTasksOnLoad() {
  fetch('/api/tasks?user_id=' + encodeURIComponent(currentUserId))
    .then(r => r.json())
    .then(data => {
      if (!data.ok) return;
      const tasks = data.tasks || [];
      updateRunningTaskBtn(tasks);

      if (!tasks.length) return;

      // 优先恢复上次看的任务，其次取进行中的任务，最后取最近的任务
      const savedTaskId = localStorage.getItem('activeTaskId');
      const running = tasks.filter(t => t.status === 'running' || t.status === 'triggering' || t.status === 'starting');
      const targetTask = (savedTaskId && tasks.find(t => t.task_id === savedTaskId))
                      || running[0]
                      || tasks[0];

      if (targetTask) {
        // 自动跳到第八步并恢复任务
        for (let i = 1; i <= 7; i++) {
          const form = document.getElementById('form' + i);
          if (form) form.style.display = 'none';
        }
        const form8 = document.getElementById('form8');
        if (form8) form8.style.display = 'block';
        updateStepStatus(7, 8);
        // prevStepBefore8 = currentStep;  // [2026-06-02] 注释
        currentStep = 8;
        startTaskListPolling();
        // 不重置 steps/logs，交给 SSE re-init 全量恢复
        if (targetTask.task_type === 'changelog') {
          switchChangelogTask(targetTask.task_id);
        } else {
          switchTask(targetTask.task_id);
        }
      }
    })
    .catch(() => {});
}

// 定时刷新任务列表（仅在第八步时）
function startTaskListPolling() {
  refreshTaskList();
  if (taskListPollTimer) clearInterval(taskListPollTimer);
  taskListPollTimer = setInterval(refreshTaskList, 5000);
}

function stopTaskListPolling() {
  if (taskListPollTimer) { clearInterval(taskListPollTimer); taskListPollTimer = null; }
}

let compilePollTimer = null;
let compileEventSource = null;  // SSE 连接
let sseFallbackTimer = null;   // SSE 失败时的轮询回退计时器

function startCompileTask(taskId, taskType) {
  activeTaskId = taskId;
  localStorage.setItem('activeTaskId', taskId);
  // 立即设置平台和任务类型（从表单已选），Jenkins 按钮不用等任务列表刷新
  window._taskType = taskType || 'build';
  window._taskPlatform = selectedPlatform || '';
  updateJenkinsBtn();
  document.getElementById('currentTaskTitle').textContent = '任务: ' + taskId;
  // 重置
  compileSteps.forEach(s => { s.status = "waiting"; s.percent = 0; });
  totalPercent = 0;
  logList = [];
  // 清空并重置日志容器
  const box = document.getElementById('compileLogBox');
  if (box) box.innerHTML = '';
  renderCompileProgress();
  // 隐藏 TSCAN 子任务进度（新任务开始时重置）
  const tscanSection = document.getElementById('tscanProgressSection');
  if (tscanSection) tscanSection.classList.add('hidden');
  // 隐藏 TSCAN 结果卡片
  const tscanResultCard = document.getElementById('tscanResultCard');
  if (tscanResultCard) tscanResultCard.classList.add('hidden');
  window._tscanBuildUrl = null;
  window._isTscanStandalone = false;
  // 恢复总进度条
  const totalWrap = document.getElementById('totalProgressWrap');
  if (totalWrap) totalWrap.classList.remove('hidden');

  toggleForm(7, 8);
  updateStepStatus(7, 8);
  // prevStepBefore8 = currentStep;  // [2026-06-02] 注释
  currentStep = 8;
  resetStopButton();  // 新任务开始，确保终止按钮可见
  startTaskListPolling();

  if (!taskId) {
    addLogAppend("⚠️ 未获取到任务ID，无法获取实时日志");
    return;
  }

  addLogAppend(`🚀 任务已提交，任务ID: ${taskId}，开始 SSE 实时日志流...`);

  // 优先使用 SSE 实时流，失败时回退到轮询
  startSSEStream(taskId);
}

// ================== SSE 实时日志流 ==================
function startSSEStream(taskId) {
  // 关闭之前的连接
  stopCurrentPolling();

  const url = `/api/tasks/${encodeURIComponent(taskId)}/stream?user_id=${encodeURIComponent(currentUserId)}`;
  console.log('[SSE] 连接实时日志流:', url);

  compileEventSource = new EventSource(url);
  let receivedCount = 0;

  compileEventSource.onmessage = function(event) {
    try {
      const msg = JSON.parse(event.data);

      // 心跳包忽略
      if (msg.heartbeat) return;

      // 初始状态恢复（重连/切换任务时后端推送完整历史）
      if (msg.init) {
        console.log('[SSE] init 消息收到, status:', msg.status, 'logs:', (msg.logs||[]).length, '行');
        // 恢复历史日志（全量重建）
        logList = [];
        const logs = msg.logs || [];
        const box = document.getElementById('compileLogBox');
        if (box) box.innerHTML = '';
        for (let i = 0; i < logs.length; i++) {
          addLogAppend(logs[i]);
        }
        // 恢复步骤进度
        if (msg.steps) {
          msg.steps.forEach((s, i) => {
            if (compileSteps[i]) {
              compileSteps[i].status = s.status;
              compileSteps[i].percent = s.percent;
            }
          });
        }
        if (msg.total_percent !== undefined) {
          totalPercent = msg.total_percent;
        }
        renderCompileProgress();
        updateStopButtonVisibility();
        // 恢复 TSCAN 子任务状态
        if (msg.tscan_status) {
          updateTscanProgress(msg.tscan_status);
        }
        // 恢复 Jenkins job URL
        if (msg.jenkins_job_url) {
          window._jenkinsJobUrl = msg.jenkins_job_url;
          updateJenkinsBtn();
        }
        // 如果任务已完成，直接触发完成逻辑
        if (msg.status && !['running', 'starting', 'triggering'].includes(msg.status)) {
          compileEventSource.close();
          compileEventSource = null;
          onTaskComplete(msg.status, msg.exit_code);
        }
        return;
      }

      // 步骤进度更新
      if (msg.steps) {
        msg.steps.forEach((s, i) => {
          if (compileSteps[i]) {
            compileSteps[i].status = s.status;
            compileSteps[i].percent = s.percent;
          }
        });
        if (msg.total_percent !== undefined) {
          totalPercent = msg.total_percent;
        }
        renderCompileProgress();
        updateStopButtonVisibility();
        return;
      }

      // TSCAN 子任务状态更新
      if (msg.tscan_status) {
        updateTscanProgress(msg.tscan_status);
        return;
      }

      // Jenkins job URL 实时更新（构建过程中捕获到 URL 后立即推送）
      if (msg.jenkins_job_url) {
        window._jenkinsJobUrl = msg.jenkins_job_url;
        updateJenkinsBtn();
        return;
      }

      // 日志行
      if (msg.line) {
        addLogAppend(msg.line);
        receivedCount++;
        updateProgressFromSSEStatus(msg.status);
      }

      // 完成信号
      if (msg.complete) {
        console.log('[SSE] 任务完成, 共接收', receivedCount, '行日志');
        compileEventSource.close();
        compileEventSource = null;
        // TSCAN 信息传递
        if (msg.tscan_build_url) window._tscanBuildUrl = msg.tscan_build_url;
        if (msg.is_tscan_standalone !== undefined) window._isTscanStandalone = msg.is_tscan_standalone;
        // 恢复 Jenkins job URL
        if (msg.jenkins_job_url) {
          window._jenkinsJobUrl = msg.jenkins_job_url;
          updateJenkinsBtn();
        }
        onTaskComplete(msg.status, msg.exit_code);
      }
    } catch (err) {
      console.error('[SSE] 消息解析失败:', err);
    }
  };

  compileEventSource.onerror = function(event) {
    console.warn('[SSE] 连接错误，回退到轮询模式');
    if (compileEventSource) {
      compileEventSource.close();
      compileEventSource = null;
    }
    // 回退到轮询模式
    startFallbackPolling(taskId);
  };

  compileEventSource.onopen = function() {
    console.log('[SSE] 连接已建立');
    // 清除回退计时器
    if (sseFallbackTimer) {
      clearTimeout(sseFallbackTimer);
      sseFallbackTimer = null;
    }
  };
}

// SSE 失败时的轮询回退
function startFallbackPolling(taskId) {
  let lastLogCount = 0;

  async function doPoll() {
    try {
      const resp = await fetch(`/api/tasks/${encodeURIComponent(taskId)}?user_id=${encodeURIComponent(currentUserId)}`, { cache: 'no-store' });
      const data = await resp.json();

      if (!data.ok) {
        addLogAppend(`⚠️ 查询任务状态失败: ${data.error || '未知错误'}`);
        sseFallbackTimer = setTimeout(doPoll, 5000);
        return;
      }

      const logs = data.log || [];
      // 只追加新行
      for (let i = lastLogCount; i < logs.length; i++) {
        addLogAppend(logs[i]);
      }
      lastLogCount = logs.length;

      const status = data.status || 'unknown';
      updateProgressFromSSEStatus(status);

      if (status === 'success' || status === 'failed' || status === 'error' || status === 'timeout') {
        onTaskComplete(status, data.exit_code);
        return;
      }

      sseFallbackTimer = setTimeout(doPoll, 2000);
    } catch (err) {
      addLogAppend(`⚠️ 轮询日志网络错误: ${err.message}，5秒后重试...`);
      sseFallbackTimer = setTimeout(doPoll, 5000);
    }
  }

  doPoll();
}

function updateProgressFromSSEStatus(status) {
  if (status === 'triggering' || status === 'running') {
    if (compileSteps[0].status !== 'success') {
      compileSteps[0].status = 'running';
    }
  }
  renderCompileProgress();
  updateStopButtonVisibility();
}

function onTaskComplete(status, exitCode) {
  if (status === 'success') {
    addLogAppend('🎉 流水线执行成功！');
    compileSteps.forEach(s => { s.status = 'success'; s.percent = 100; });
    totalPercent = 100;

    // TSCAN 构建链接展示
    const tscanUrl = window._tscanBuildUrl;
    const isStandalone = window._isTscanStandalone;
    if (tscanUrl) {
      const card = document.getElementById('tscanResultCard');
      const title = document.getElementById('tscanResultTitle');
      const body = document.getElementById('tscanResultBody');
      if (card && body) {
        card.classList.remove('hidden');
        if (isStandalone) {
          if (title) title.textContent = 'TSCAN 构建完成';
          body.innerHTML = '可点击查看下载 TSCAN 产物：<br><a href="' + tscanUrl + '" target="_blank" class="text-blue-600 underline break-all">' + tscanUrl + '</a>';
        } else {
          if (title) title.textContent = 'TSCAN 产物已更新到飞书文档';
          body.innerHTML = 'TSCAN 产物已上传 NAS 并更新到飞书文档。<br>可点击查看下载 TSCAN 产物：<br><a href="' + tscanUrl + '" target="_blank" class="text-blue-600 underline break-all">' + tscanUrl + '</a>';
        }
      }
    }
    window._tscanBuildUrl = null;
    window._isTscanStandalone = false;

    // 成功后自动保存 Wiki Token 到 platform_config.js（如果设备尚无预设 Token）
    saveWikiTokenIfNew();
    // 成功后自动保存手动输入的新目标项目到 platform_config.js
    savePlatformDeviceIfNew();
    // 成功后自动保存新发版分支到 platform_config.js
    saveBranchIfNew();
  } else if (status === 'terminated') {
    addLogAppend('🛑 任务已被手动终止');
    compileSteps.forEach(s => { if (s.status !== 'success') { s.status = 'failed'; s.percent = 0; } });
  } else if (status === 'failed') {
    addLogAppend(`❌ 流水线执行失败（退出码: ${exitCode || 'unknown'}）`);
    compileSteps.forEach(s => { if (s.status !== 'success') { s.status = 'failed'; } });
  } else if (status === 'timeout') {
    addLogAppend('⏰ 流水线执行超时');
    compileSteps.forEach(s => { if (s.status !== 'success') { s.status = 'failed'; } });
  } else {
    addLogAppend('❌ 流水线异常终止');
    compileSteps.forEach(s => { if (s.status !== 'success') { s.status = 'failed'; } });
  }
  renderCompileProgress();
  updateStopButtonVisibility();
  stopCurrentPolling();
}

function stopCurrentPolling() {
  // 关闭 SSE
  if (compileEventSource) {
    compileEventSource.close();
    compileEventSource = null;
  }
  // 清除回退轮询
  if (sseFallbackTimer) {
    clearTimeout(sseFallbackTimer);
    sseFallbackTimer = null;
  }
  // 清除旧版轮询（兼容）
  if (compilePollTimer) {
    clearTimeout(compilePollTimer);
    compilePollTimer = null;
  }
}

// 阶段名称映射（步骤索引 → 按钮文字）
const PHASE_NAMES = ['终止版本编译', '终止本地下载', '终止NAS文件上传', '终止分享链接', '终止生成文档'];

async function stopCurrentPhase() {
  if (!activeTaskId) return;
  // 找到当前正在运行的步骤
  const runningIdx = compileSteps.findIndex(s => s.status === 'running');
  const phaseName = runningIdx >= 0 ? PHASE_NAMES[runningIdx] : '当前阶段';
  if (!confirm(`确定要${phaseName}吗？点击确定后将终止当前任务。`)) return;
  try {
    const resp = await fetch(`/api/tasks/${encodeURIComponent(activeTaskId)}/stop`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ user_id: currentUserId }),
    });
    const data = await resp.json();
    if (data.ok) {
      addLogAppend(`🛑 ${phaseName}成功，任务已终止`);
      stopCurrentPolling();
      compileSteps.forEach(s => { if (s.status !== 'success') { s.status = 'failed'; s.percent = 0; } });
      renderCompileProgress();
      // 隐藏终止按钮
      hideAllStopButtons();
    } else {
      addLogAppend(`⚠️ ${phaseName}失败: ${data.error || '未知错误'}`);
    }
  } catch (err) {
    addLogAppend(`⚠️ ${phaseName}请求失败: ${err.message}`);
  }
}

// 根据当前编译步骤状态，控制两个终止按钮和状态按钮
function updateStopButtonVisibility() {
  const btnBuild = document.getElementById('btnStopBuild');
  const btnPhase = document.getElementById('btnStopPhase');
  const phaseText = document.getElementById('btnStopPhaseText');

  // 找到当前正在运行的步骤
  const runningIdx = compileSteps.findIndex(s => s.status === 'running');
  
  if (runningIdx === -1) {
    // 没有正在运行的步骤（全部 waiting / success / failed）
    if (btnBuild) btnBuild.classList.add('hidden');
    if (btnPhase) btnPhase.classList.add('hidden');
    return;
  }

  if (runningIdx === 0) {
    // 版本编译中：只显示终止按钮（Changelog/TSCAN 覆盖文字）
    if (btnBuild) {
      btnBuild.classList.remove('hidden');
      if (clActiveTaskId) {
        btnBuild.innerHTML = '<i class="fa fa-stop"></i> 终止 Changelog 构建';
      } else if (window._isTscanStandalone) {
        btnBuild.innerHTML = '<i class="fa fa-stop"></i> 终止 TSCAN 构建';
      }
    }
    if (btnPhase) btnPhase.classList.add('hidden');
  } else {
    // 其他阶段：只显示动态阶段按钮
    if (btnBuild) btnBuild.classList.add('hidden');
    if (btnPhase) {
      btnPhase.classList.remove('hidden');
      if (clActiveTaskId) {
        if (phaseText) phaseText.textContent = '终止 Changelog 构建';
      } else if (window._isTscanStandalone) {
        if (phaseText) phaseText.textContent = '终止 TSCAN 构建';
      } else if (phaseText && runningIdx < PHASE_NAMES.length) {
        phaseText.textContent = PHASE_NAMES[runningIdx];
      }
    }
  }
}

function hideAllStopButtons() {
  const btnBuild = document.getElementById('btnStopBuild');
  const btnPhase = document.getElementById('btnStopPhase');
  // [2026-06-02] 注释：btnBackFrom8 已从 HTML 移除
  // const btnBack = document.getElementById('btnBackFrom8');
  if (btnBuild) btnBuild.classList.add('hidden');
  if (btnPhase) btnPhase.classList.add('hidden');
  // if (btnBack) btnBack.classList.add('hidden');
}

// Jenkins 跳转按钮（静态 URL，基于平台选择）
const JENKINS_PLATFORM_URLS = {
  mhs003:  'https://jenkins.huami.com/job/firmware_auto_trigger/job/HuamiOS_HS3/',
  mhs003s: 'https://jenkins.huami.com/job/firmware_auto_trigger/job/HuamiOS_multi_platform/',
};

function openJenkinsJob() {
  const platform = window._taskPlatform;
  const url = JENKINS_PLATFORM_URLS[platform] || window._jenkinsJobUrl;
  if (url) window.open(url, '_blank');
}

function updateJenkinsBtn() {
  const btn = document.getElementById('btnOpenJenkins');
  if (!btn) return;
  const platform = window._taskPlatform;
  const taskType = window._taskType;
  // 仅 build / tscan / skip_jenkins 任务显示 Jenkins 按钮
  const showForTask = taskType === 'build' || taskType === 'tscan' || taskType === 'skip_jenkins' || !taskType;
  const url = JENKINS_PLATFORM_URLS[platform] || window._jenkinsJobUrl;
  if (url && showForTask) {
    btn.classList.remove('hidden');
  } else {
    btn.classList.add('hidden');
  }
}

// 新任务开始时重置按钮状态
function resetStopButton() {
  hideAllStopButtons();
  window._jenkinsJobUrl = '';
  window._taskPlatform = '';
  window._taskType = '';
  updateJenkinsBtn();
}

/* [2026-06-02] 注释：btnBackFrom8 按钮已删除，此函数无调用者
// 从第8步返回上一步（任务失败/终止后使用）
function goBackFromStep8() {
  const target = prevStepBefore8 || 7;
  stopTaskListPolling();
  stopCurrentPolling();

  // 隐藏第8步
  const form8 = document.getElementById('form8');
  if (form8) form8.style.display = 'none';

  // 显示目标步骤
  const targetForm = document.getElementById('form' + target);
  if (targetForm) targetForm.style.display = 'block';

  // 显示回左侧步骤进度条
  for (let i = 1; i <= 7; i++) {
    const form = document.getElementById('form' + i);
    if (form && i !== target) form.style.display = 'none';
  }

  updateStepStatus(8, target);
  currentStep = target;
  hideAllStopButtons();

  // 隐藏"返回"按钮
  const btn = document.getElementById('btnBackFrom8');
  if (btn) btn.classList.add('hidden');
}
*/

// TSCAN 子任务进度更新
function updateTscanProgress(ts) {
  const section = document.getElementById('tscanProgressSection');
  if (!section || !ts) return;
  if (!ts.enabled) { section.classList.add('hidden'); return; }
  section.classList.remove('hidden');

  const icon = document.getElementById('tscanStatusIcon');
  const badge = document.getElementById('tscanStatusBadge');
  const msg = document.getElementById('tscanMessage');
  const bStep = document.getElementById('tscanStepBuild');
  const dStep = document.getElementById('tscanStepDownload');
  const uStep = document.getElementById('tscanStepUpload');

  // 重置所有步骤样式
  [bStep, dStep, uStep].forEach(s => { if (s) s.className = 'tscan-step'; });

  const status = ts.status || 'pending';
  if (status === 'completed') {
    if (icon) { icon.className = 'fa fa-check-circle text-green-500 text-sm'; }
    if (badge) { badge.textContent = '已完成'; badge.className = 'text-xs px-2 py-0.5 rounded-full bg-green-100 text-green-600'; }
    if (msg) msg.textContent = ts.message || 'TSCAN 产物已上传 NAS，飞书文档已更新';
    [bStep, dStep, uStep].forEach(s => { if (s) s.className = 'tscan-step done'; });
  } else if (status === 'failed' || status.startsWith('error')) {
    if (icon) { icon.className = 'fa fa-times-circle text-red-500 text-sm'; }
    if (badge) { badge.textContent = '失败'; badge.className = 'text-xs px-2 py-0.5 rounded-full bg-red-100 text-red-600'; }
    if (msg) msg.textContent = ts.message || 'TSCAN 任务失败';
  } else {
    if (icon) { icon.className = 'fa fa-cog fa-spin text-blue-500 text-sm'; }
    if (badge) { badge.textContent = '进行中'; badge.className = 'text-xs px-2 py-0.5 rounded-full bg-blue-100 text-blue-600'; }
    if (msg) msg.textContent = ts.message || '';

    // 根据子状态高亮当前步骤
    if (status === 'building') {
      if (bStep) bStep.className = 'tscan-step active';
    } else if (status === 'downloading') {
      if (bStep) bStep.className = 'tscan-step done';
      if (dStep) dStep.className = 'tscan-step active';
    } else if (status === 'uploading') {
      if (bStep) bStep.className = 'tscan-step done';
      if (dStep) dStep.className = 'tscan-step done';
      if (uStep) uStep.className = 'tscan-step active';
    }
  }
}

// ================== 增量日志渲染（append-only，不重建 DOM）==================
function addLogAppend(msg) {
  if (!msg) return;
  const timestamp = new Date().toLocaleTimeString('zh-CN', { hour12: false });
  logList.push({ time: timestamp, text: msg });

  const box = document.getElementById('compileLogBox');
  if (!box) return;

  // 增量追加一行，不再重建整个 innerHTML
  let cls = 'terminal-line terminal-info';
  if (msg.includes('ERROR') || msg.includes('error') || msg.includes('❌') || msg.includes('FAILURE') || msg.includes('failed')) {
    cls = 'terminal-line terminal-error';
  } else if (msg.includes('WARN') || msg.includes('WARNING') || msg.includes('⚠️')) {
    cls = 'terminal-line terminal-warn';
  } else if (msg.includes('SUCCESS') || msg.includes('✅') || msg.includes('🎉') || msg.includes('success')) {
    cls = 'terminal-line terminal-success';
  } else if (msg.includes('Trigger') || msg.includes('trigger') || msg.includes('== ')) {
    cls = 'terminal-line terminal-highlight';
  } else if (msg.includes('Running:') || msg.includes('Running')) {
    cls = 'terminal-line terminal-cmd';
  }

  const line = document.createElement('div');
  line.className = cls;
  line.innerHTML = `<span class="terminal-time">[${timestamp}]</span> ${escapeHtml(msg)}`;
  box.appendChild(line);

  // 限制 DOM 中最多保留 500 行，防止内存爆炸
  while (box.children.length > 500) {
    box.removeChild(box.firstChild);
  }

  // 自动滚动到底部
  box.scrollTop = box.scrollHeight;
}

/* [2026-06-02] 注释：无调用者
// 保留旧的 addLog 兼容其他调用方（已经是增量渲染）
function addLog(msg) {
  addLogAppend(msg);
}
*/

// ================== 第八步 UI 渲染函数 ==================

/* [2026-06-02] 注释：被第3221行同名函数覆盖，此定义无效
function escapeHtml(str) {
  const div = document.createElement('div');
  div.textContent = str;
  return div.innerHTML;
}
*/

let _stepUIDirty = true;  // 标记是否需要重建步骤 UI
let _lastStepStatuses = [];  // 上次渲染时的步骤状态

function renderCompileProgress() {
  // 检测步骤状态是否变化（需要重建 DOM）
  if (!_stepUIDirty) {
    for (let i = 0; i < compileSteps.length; i++) {
      if (_lastStepStatuses[i] !== compileSteps[i].status) {
        _stepUIDirty = true;
        break;
      }
    }
  }
  // 总进度
  const percentText = document.getElementById('totalPercentText');
  const progressBar = document.getElementById('totalProgressBar');
  if (percentText) percentText.textContent = totalPercent + '%';
  if (progressBar) progressBar.style.width = totalPercent + '%';

  // 各步骤进度卡片
  const container = document.getElementById('compileStepsUI');
  if (!container) return;

  const statusIcon = {
    waiting: '<span class="step-status-icon status-waiting"><i class="fa fa-clock-o"></i></span>',
    running: '<span class="step-status-icon status-running"><i class="fa fa-spinner fa-spin"></i></span>',
    success: '<span class="step-status-icon status-success"><i class="fa fa-check-circle"></i></span>',
    failed: '<span class="step-status-icon status-failed"><i class="fa fa-times-circle"></i></span>',
    error:  '<span class="step-status-icon status-failed"><i class="fa fa-times-circle"></i></span>'
  };

  // 首次渲染或状态结构变化时才重建 DOM
  if (_stepUIDirty || container.children.length !== compileSteps.length) {
    _stepUIDirty = false;
    container.innerHTML = compileSteps.map((s, i) => {
      const iconHtml = statusIcon[s.status] || statusIcon.waiting;
      const percentHtml = s.percent > 0 ? `<span class="step-percent" id="stepPct${i}">${s.percent}%</span>` : `<span class="step-percent" id="stepPct${i}"></span>`;
      // Changelog 紫色，TSCAN 绿色，默认蓝色
      const isCl = !!clActiveTaskId;
      const isTscan = !!window._isTscanStandalone;
      let barBg;
      if (s.status === 'success') {
        barBg = 'bg-success';
      } else if (s.status === 'failed') {
        barBg = 'bg-red-500';
      } else if (s.status === 'running') {
        barBg = isCl ? 'bg-purple-500' : (isTscan ? 'bg-green-500' : 'bg-primary');
      } else {
        barBg = 'bg-gray-300';
      }
      return `
        <div class="compile-step-item">
          <div class="step-header">${iconHtml}<span class="step-name">${s.name}</span>${percentHtml}</div>
          <div class="step-bar-wrap"><div class="step-bar ${barBg}" id="stepBar${i}" style="width:${s.percent}%; transition: width 0.5s ease-out"></div></div>
        </div>
      `;
    }).join('');
    _lastStepStatuses = compileSteps.map(s => s.status);
  } else {
    // 增量更新：只改宽度和百分比
    compileSteps.forEach((s, i) => {
      const bar = document.getElementById('stepBar' + i);
      const pct = document.getElementById('stepPct' + i);
      if (bar) bar.style.width = s.percent + '%';
      if (pct) pct.textContent = s.percent > 0 ? s.percent + '%' : '';
    });
  }
}

// ================== Header 滚动显隐 ==================
document.addEventListener('DOMContentLoaded', function() {
  const header = document.querySelector('.top-nav');
  if (!header) return;

  let ticking = false;
  const HEADER_HEIGHT = 70;
  const SCROLL_THRESHOLD = 10;

  function hasScrollbar() {
    return document.documentElement.scrollHeight > window.innerHeight + SCROLL_THRESHOLD;
  }

  function onScroll() {
    if (!hasScrollbar()) {
      header.classList.remove('hidden-header');
      return;
    }

    if (window.scrollY <= HEADER_HEIGHT) {
      header.classList.remove('hidden-header');
    } else {
      header.classList.add('hidden-header');
    }
  }

  window.addEventListener('scroll', function() {
    if (!ticking) {
      requestAnimationFrame(function() {
        onScroll();
        ticking = false;
      });
      ticking = true;
    }
  }, { passive: true });

  onScroll();
});

// ================== 运维信息弹窗 ==================
document.addEventListener('DOMContentLoaded', function() {
  const trigger = document.getElementById('infoTrigger');
  const overlay = document.getElementById('infoOverlay');
  const closeBtn = document.getElementById('infoClose');
  if (!trigger || !overlay || !closeBtn) return;

  trigger.addEventListener('click', function() {
    overlay.classList.add('show');
  });

  closeBtn.addEventListener('click', function() {
    overlay.classList.remove('show');
  });

  overlay.addEventListener('click', function(e) {
    if (e.target === overlay) overlay.classList.remove('show');
  });

  document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape' && overlay.classList.contains('show')) {
      overlay.classList.remove('show');
    }
  });
});

// ================== 数据统计 ==================
function showStatsModal() {
  document.getElementById('statsOverlay').classList.remove('hidden');
  loadStats();
}

function closeStatsModal() {
  document.getElementById('statsOverlay').classList.add('hidden');
}

async function loadStats() {
  try {
    const [summaryResp, eventsResp] = await Promise.all([
      fetch('/api/stats'), fetch('/api/stats/events')
    ]);
    const summary = (await summaryResp.json()).summary || {};
    const events = (await eventsResp.json()).events || [];

    // 汇总卡片
    const typeNames = { build: '编译版本', tscan: 'TSCAN', skip_jenkins: '生成文档', changelog: 'Changelog' };
    const icons = { build: '🚀', tscan: '🔍', skip_jenkins: '📄', changelog: '📊' };
    const colors = { build: '#165DFF', tscan: '#00B42A', skip_jenkins: '#F7BA1E', changelog: '#722ED1' };

    let summaryHTML = `<div style="background:#f0f5ff;border-radius:12px;padding:1rem 1.5rem;text-align:center;min-width:120px">
      <div style="font-size:2rem;font-weight:800;color:#165DFF">${summary.total || 0}</div>
      <div style="font-size:0.85rem;color:#6b7280">总操作次数</div>
    </div>`;

    for (const [type, name] of Object.entries(typeNames)) {
      const count = (summary.by_type || {})[type] || 0;
      summaryHTML += `<div style="background:#f9fafb;border-radius:12px;padding:1rem 1.5rem;text-align:center;min-width:100px;border:2px solid ${colors[type]}20">
        <div style="font-size:1.5rem;font-weight:700;color:${colors[type]}">${icons[type]} ${count}</div>
        <div style="font-size:0.8rem;color:#6b7280">${name}</div>
      </div>`;
    }
    document.getElementById('statsSummary').innerHTML = summaryHTML;

    // 事件列表
    let rows = '';
    events.forEach(e => {
      let operator = e.operator || '未知';
      if (operator.startsWith('feishu_bot_')) {
        operator = '🤖 群机器人';
      }
      rows += `<tr style="border-bottom:1px solid #f3f4f6">
        <td style="padding:0.5rem 0.75rem;color:#374151">${escapeHtml(operator)}</td>
        <td style="padding:0.5rem 0.75rem;color:#4b5563">${e.type_cn || e.type || ''}</td>
        <td style="padding:0.5rem 0.75rem;color:#6b7280">${escapeHtml(e.device || '')}</td>
        <td style="padding:0.5rem 0.75rem;color:#9ca3af;white-space:nowrap">${e.time || ''}</td>
      </tr>`;
    });
    document.getElementById('statsEventsBody').innerHTML = rows || '<tr><td colspan="4" style="padding:2rem;text-align:center;color:#9ca3af">暂无数据</td></tr>';
  } catch (err) {
    console.error('加载统计数据失败:', err);
  }
}

function escapeHtml(str) {
  const div = document.createElement('div');
  div.textContent = str;
  return div.innerHTML;
}
