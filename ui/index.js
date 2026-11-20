// QwenPaw Node Bridge 的 SSH 设置面板。
//
// 这个文件由 Console 通过 /api/frontend_plugin/... 直接动态 import，没有构建步骤，
// 因此只能写普通 ESM 与 React.createElement，不能写 JSX。
// 宿主注入 window.QwenPaw.host：React、antd、getApiUrl、getApiToken。
const host = window.QwenPaw.host;
const React = host.React;
const antd = host.antd;
const h = React.createElement;
const { Alert, Button, Card, Checkbox, Divider, Input, InputNumber, Radio, Space, Typography, message } = antd;
const Text = Typography.Text;
const Paragraph = Typography.Paragraph;

// 设置菜单图标。宿主对字符串图标只做 <span> 文本渲染，传 React 元素才会按图形画；
// 画一个终端窗口加提示符，和「SSH 中继」这个功能对得上。
function BridgeIcon() {
  return h(
    "svg",
    {
      width: 16,
      height: 16,
      viewBox: "0 0 24 24",
      fill: "none",
      stroke: "currentColor",
      strokeWidth: 1.8,
      strokeLinecap: "round",
      strokeLinejoin: "round",
      "aria-hidden": "true",
      style: { display: "block" },
    },
    h("rect", { x: 3, y: 4, width: 18, height: 16, rx: 2.5 }),
    h("path", { d: "M7 10l2.6 2.6L7 15.2" }),
    h("path", { d: "M13 15.4h4" }),
  );
}

// host.getApiUrl 会自行补上 /api 前缀，这里不能再带，否则请求会变成 /api/api/...
const SSH_API_PATH = "/plugins/qwenpaw-node-bridge/ssh/settings";
const PAIRING_API_PATH = "/plugins/qwenpaw-node-bridge/pairing/tokens";

async function request(path, method, body, options) {
  const headers = {};
  const token = host.getApiToken ? host.getApiToken() : "";
  if (token) headers.Authorization = "Bearer " + token;
  if (options && options.trusted) headers["X-QwenPaw-Trusted-Host"] = "1";
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const response = await fetch(host.getApiUrl(path), {
    method,
    headers,
    credentials: "same-origin",
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await response.text();
  if (!response.ok) {
    let detail = text;
    try {
      const parsed = JSON.parse(text);
      detail = parsed.detail || text;
    } catch (_) {
      // 保持原文
    }
    throw new Error(detail || `HTTP ${response.status}`);
  }
  return text ? JSON.parse(text) : {};
}

function Field(props) {
  return h(
    "div",
    { style: { marginBottom: 16 } },
    h("div", { style: { marginBottom: 6, fontWeight: 500 } }, props.label),
    props.children,
    props.hint
      ? h(Text, { type: "secondary", style: { display: "block", marginTop: 4, fontSize: 12 } }, props.hint)
      : null,
  );
}

function SshSettingsPanel() {
  const [loading, setLoading] = React.useState(true);
  const [saving, setSaving] = React.useState(false);
  const [error, setError] = React.useState("");
  const [configured, setConfigured] = React.useState(false);
  const [keyConfigured, setKeyConfigured] = React.useState(false);
  const [passwordConfigured, setPasswordConfigured] = React.useState(false);
  const [passphraseConfigured, setPassphraseConfigured] = React.useState(false);
  const [form, setForm] = React.useState({
    host: "127.0.0.1",
    port: 22,
    user: "",
    auth: "key",
    key_source: "file",
    key_path: "",
  });
  const [privateKey, setPrivateKey] = React.useState("");
  const [password, setPassword] = React.useState("");
  const [passphrase, setPassphrase] = React.useState("");
  // 未开 Web 认证时，公网地址需要用户在这里显式确认「仅我可访问」；默认不勾。
  const [trusted, setTrusted] = React.useState(false);

  const update = (patch) => setForm((current) => ({ ...current, ...patch }));

  async function load() {
    setLoading(true);
    setError("");
    try {
      const data = await request(SSH_API_PATH, "GET");
      setForm({
        host: data.host || "127.0.0.1",
        port: data.port || 22,
        user: data.user || "",
        auth: data.auth || "key",
        key_source: data.key_source || "file",
        key_path: data.key_path || "",
      });
      setConfigured(Boolean(data.configured));
      setKeyConfigured(Boolean(data.key_configured));
      setPasswordConfigured(Boolean(data.password_configured));
      setPassphraseConfigured(Boolean(data.passphrase_configured));
    } catch (err) {
      setError(String(err.message || err));
    } finally {
      setLoading(false);
    }
  }

  React.useEffect(() => {
    load();
  }, []);

  async function save() {
    setError("");
    if (!form.host.trim()) {
      setError("主机地址不能为空");
      return;
    }
    if (!form.user.trim()) {
      setError("用户名不能为空");
      return;
    }
    if (form.auth === "key" && form.key_source === "file" && !form.key_path.trim()) {
      setError("密钥认证需要填写服务端私钥路径");
      return;
    }
    const body = {
      host: form.host.trim(),
      port: Number(form.port) || 22,
      user: form.user.trim(),
      auth: form.auth,
      key_source: form.key_source,
      key_path: form.key_path.trim(),
    };
    if (privateKey.trim()) body.private_key = privateKey;
    if (password) body.password = password;
    if (passphrase) body.passphrase = passphrase;
    setSaving(true);
    try {
      const data = await request(SSH_API_PATH, "PUT", body, { trusted });
      setKeyConfigured(Boolean(data.key_configured));
      setPasswordConfigured(Boolean(data.password_configured));
      setPassphraseConfigured(Boolean(data.passphrase_configured));
      setConfigured(Boolean(data.configured));
      setPrivateKey("");
      setPassword("");
      setPassphrase("");
      message.success("SSH 设置已保存");
    } catch (err) {
      setError(String(err.message || err));
    } finally {
      setSaving(false);
    }
  }

  if (loading) {
    return h("div", null, h(Text, { type: "secondary" }, "正在读取设置…"));
  }

  return h(
    "div",
    null,
    location.protocol === "http:"
      ? h(Alert, {
          type: "warning",
          showIcon: true,
          style: { marginBottom: 16 },
          message: "当前页面使用 HTTP",
          description:
            "私钥与口令会在明文链路上传输。请改用 HTTPS，或只在可信网络内进行配置。",
        })
      : null,
    error
      ? h(Alert, {
          type: "error",
          showIcon: true,
          style: { marginBottom: 16 },
          message: error,
        })
      : null,
    h(
      Paragraph,
      { type: "secondary" },
      "保存后手机端可经配套插件中继到该目标。未开启 Web 登录认证时，仅私有/回环地址或下方显式勾选「此地址仅我可访问」的地址可以读取凭据与保存设置；公网部署请开启登录认证与 HTTPS。该勾选只是用户确认，不是密码学保证。",
    ),
    h(
      Space,
      { direction: "vertical", size: 0, style: { width: "100%" } },
      h(
        Field,
        { label: "主机地址", hint: "默认 127.0.0.1；填其它主机即表示允许中继到该主机。" },
        h(Input, {
          value: form.host,
          onChange: (event) => update({ host: event.target.value }),
          placeholder: "127.0.0.1",
        }),
      ),
      h(
        Field,
        { label: "端口" },
        h(InputNumber, {
          min: 1,
          max: 65535,
          value: form.port,
          onChange: (value) => update({ port: value || 22 }),
          style: { width: 160 },
        }),
      ),
      h(
        Field,
        { label: "用户名" },
        h(Input, {
          value: form.user,
          onChange: (event) => update({ user: event.target.value }),
          placeholder: "例如 csdetr",
        }),
      ),
      h(
        Field,
        { label: "认证方式" },
        h(
          Radio.Group,
          {
            value: form.auth,
            onChange: (event) => update({ auth: event.target.value }),
          },
          h(Radio.Button, { value: "key" }, "密钥"),
          h(Radio.Button, { value: "password" }, "密码"),
        ),
      ),
      form.auth === "key"
        ? h(
            "div",
            null,
            h(
              Field,
              {
                label: "私钥来源",
                hint: "优先使用服务端已有路径，避免在插件目录里多留一份私钥。",
              },
              h(
                Radio.Group,
                {
                  value: form.key_source,
                  onChange: (event) => update({ key_source: event.target.value }),
                },
                h(Radio.Button, { value: "file" }, "服务端已有路径"),
                h(Radio.Button, { value: "inline" }, "粘贴私钥"),
              ),
            ),
            form.key_source === "file"
              ? h(
                  Field,
                  {
                    label: "私钥路径",
                    hint: keyConfigured ? "当前私钥已配置；修改路径后保存即生效。" : "例如 ~/.ssh/id_ed25519",
                  },
                  h(Input, {
                    value: form.key_path,
                    onChange: (event) => update({ key_path: event.target.value }),
                    placeholder: "~/.ssh/id_ed25519",
                  }),
                )
              : h(
                  Field,
                  {
                    label: "私钥内容",
                    hint: "留空表示不修改。粘贴方式会以 0600 权限写入插件数据目录。",
                  },
                  h(Input.TextArea, {
                    value: privateKey,
                    onChange: (event) => setPrivateKey(event.target.value),
                    rows: 6,
                    spellCheck: false,
                    placeholder: "-----BEGIN OPENSSH PRIVATE KEY-----",
                  }),
                ),
            h(
              Field,
              {
                label: "私钥口令",
                hint: passphraseConfigured ? "当前口令已配置；留空表示不修改。" : "私钥没有口令时留空。",
              },
              h(Input.Password, {
                value: passphrase,
                onChange: (event) => setPassphrase(event.target.value),
                autoComplete: "new-password",
                placeholder: passphraseConfigured ? "已配置" : "未配置",
              }),
            ),
          )
        : h(
            Field,
            {
              label: "登录密码",
              hint: passwordConfigured ? "当前密码已配置；留空表示不修改。" : "保存后不会回显。",
            },
            h(Input.Password, {
              value: password,
              onChange: (event) => setPassword(event.target.value),
              autoComplete: "new-password",
              placeholder: passwordConfigured ? "已配置" : "未配置",
            }),
          ),
      h(Divider, null),
      h(
        Text,
        { type: "secondary", style: { display: "block", marginBottom: 12 } },
        configured
          ? `目标已配置：${form.user || "（未填用户名）"}@${form.host}:${form.port}`
          : "目标尚未配置。",
      ),
      h(
        Checkbox,
        {
          checked: trusted,
          onChange: (event) => setTrusted(Boolean(event.target.checked)),
          style: { marginBottom: 12 },
        },
        "此地址仅我可访问（未开 Web 认证时需要）",
      ),
      h(
        Button,
        { type: "primary", loading: saving, onClick: save },
        "保存",
      ),
    ),
  );
}

/**
 * 设备配对面板。
 *
 * 签发一枚移动端访问令牌并画成二维码。二维码里同时带服务器地址与令牌，
 * 手机扫码即可建立实例。令牌只显示一次，登记表里只有元数据。
 */
function PairingPanel() {
  const [error, setError] = React.useState("");
  const [issuing, setIssuing] = React.useState(false);
  const [days, setDays] = React.useState(30);
  const [label, setLabel] = React.useState("");
  const [addressText, setAddressText] = React.useState(
    typeof window !== "undefined" && window.location ? window.location.origin : "",
  );
  const [issued, setIssued] = React.useState(null);
  const [items, setItems] = React.useState([]);
  const [editing, setEditing] = React.useState(null);
  const [listed, setListed] = React.useState(false);

  function parseAddresses() {
    return addressText
      .split(/\s+/)
      .map((line) => line.trim())
      .filter(Boolean);
  }

  async function loadList() {
    try {
      const data = await request(PAIRING_API_PATH, "GET");
      setItems(Array.isArray(data.items) ? data.items : []);
      setListed(true);
    } catch (err) {
      setError(String(err.message || err));
    }
  }

  React.useEffect(() => {
    loadList();
  }, []);

  async function issue() {
    setError("");
    const addresses = parseAddresses();
    if (!addresses.length) {
      setError("至少填写一个服务器地址");
      return;
    }
    setIssuing(true);
    try {
      const data = await request(PAIRING_API_PATH, "POST", {
        expires_in_days: days,
        label: label.trim(),
        addresses,
      });
      setIssued(data);
      await loadList();
    } catch (err) {
      setError(String(err.message || err));
    } finally {
      setIssuing(false);
    }
  }

  async function revoke(jti) {
    setError("");
    try {
      await request(PAIRING_API_PATH + "/" + encodeURIComponent(jti) + "/revoke", "POST");
      message.success("已吊销该令牌");
      await loadList();
    } catch (err) {
      setError(String(err.message || err));
    }
  }

  async function rename(jti, label) {
    setError("");
    try {
      await request(PAIRING_API_PATH + "/" + encodeURIComponent(jti), "PATCH", { label });
      message.success("名称已更新");
      setEditing(null);
      await loadList();
    } catch (err) {
      setError(String(err.message || err));
    }
  }

  async function remove(jti) {
    setError("");
    try {
      await request(PAIRING_API_PATH + "/" + encodeURIComponent(jti), "DELETE");
      message.success("已删除该条目");
      await loadList();
    } catch (err) {
      setError(String(err.message || err));
    }
  }

  function copyToken() {
    if (!issued || !issued.token) return;
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(issued.token).then(
        () => message.success("令牌已复制"),
        () => setError("复制失败，请手动选择文本"),
      );
    } else {
      setError("当前浏览器不支持自动复制，请手动选择文本");
    }
  }

  function expiryText(epochSeconds) {
    if (!epochSeconds) return "未知";
    const date = new Date(epochSeconds * 1000);
    if (Number.isNaN(date.getTime())) return "未知";
    const pad = (n) => String(n).padStart(2, "0");
    return (
      date.getFullYear() +
      "-" +
      pad(date.getMonth() + 1) +
      "-" +
      pad(date.getDate()) +
      " " +
      pad(date.getHours()) +
      ":" +
      pad(date.getMinutes())
    );
  }

  const qrValue = issued && issued.payload ? issued.payload : "";

  return h(
    "div",
    null,
    error ? h(Alert, { type: "error", showIcon: true, message: error, style: { marginBottom: 16 } }) : null,
    h(
      Alert,
      {
        type: "warning",
        showIcon: true,
        style: { marginBottom: 16 },
        message: "二维码等同于凭据，请勿截图外传。",
        description:
          "令牌在生成时显示一次，关闭后无法再次查看。服务端开启反向代理的 Basic 验证时，令牌会随请求出现在访问日志中。",
      },
    ),
    h(
      Field,
      { label: "有效期", hint: "到期后需要重新生成。长期令牌不会自动失效，泄露风险最高。" },
      h(
        Radio.Group,
        {
          value: days,
          onChange: (event) => setDays(event.target.value),
          optionType: "button",
        },
        h(Radio.Button, { value: 7 }, "7 天"),
        h(Radio.Button, { value: 30 }, "30 天"),
        h(Radio.Button, { value: 90 }, "90 天"),
        h(Radio.Button, { value: 0 }, "长期"),
      ),
    ),
    h(
      Field,
      { label: "服务器地址", hint: "一行一个。第一条为默认地址，其余作为备用按顺序回退。" },
      h(Input.TextArea, {
        rows: 2,
        value: addressText,
        onChange: (event) => setAddressText(event.target.value),
        placeholder: "http://192.168.1.10:8088",
      }),
    ),
    h(
      Field,
      { label: "用途说明（可选）", hint: "只写进登记表，便于以后辨认是哪台设备。" },
      h(Input, {
        value: label,
        maxLength: 64,
        onChange: (event) => setLabel(event.target.value),
        placeholder: "例如：主力手机",
      }),
    ),
    h(Button, { type: "primary", loading: issuing, onClick: issue }, "生成配对码"),
    issued
      ? h(
          "div",
          { style: { marginTop: 20 } },
          h("div", { style: { fontWeight: 500, marginBottom: 8 } }, "配对二维码"),
          qrValue && antd.QRCode
            ? h(antd.QRCode, { value: qrValue, size: 200 })
            : issued.qrcode_img
              ? h("img", {
                  alt: "配对二维码",
                  width: 200,
                  src: "data:image/png;base64," + issued.qrcode_img,
                })
              : h(Text, { type: "secondary" }, "二维码图片生成失败，请使用下方令牌。"),
          h(
            Text,
            { type: "secondary", style: { display: "block", margin: "12px 0 6px" } },
            "访问令牌（仅显示一次）",
          ),
          h(Input.TextArea, { rows: 3, readOnly: true, value: issued.token || "" }),
          h(
            Button,
            { style: { marginTop: 8 }, onClick: copyToken },
            "复制令牌",
          ),
        )
      : null,
    h(Divider, null),
    h(
      "div",
      { style: { display: "flex", alignItems: "center", marginBottom: 8 } },
      h("div", { style: { fontWeight: 500 } }, "已签发的令牌"),
      h(
        Button,
        { type: "link", size: "small", onClick: loadList, style: { marginLeft: 8 } },
        "刷新",
      ),
    ),
    listed && items.length === 0
      ? h(Text, { type: "secondary" }, "暂无记录。服务端未启用 Web 登录时不会签发令牌。")
      : null,
    items.length
      ? h(
          "div",
          null,
          ...items.map((item) =>
            h(
              "div",
              {
                key: item.jti,
                style: {
                  display: "flex",
                  alignItems: "center",
                  padding: "8px 0",
                  borderBottom: "1px solid rgba(0,0,0,0.06)",
                },
              },
              h(
                "div",
                { style: { flex: 1 } },
                h("div", null, item.label || "未命名"),
                h(
                  Text,
                  { type: "secondary", style: { fontSize: 12 } },
                  "到期：" +
                    expiryText(item.expires_at) +
                    " · 签发：" +
                    expiryText(item.issued_at) +
                    (item.revoked ? " · 已吊销" : ""),
                ),
              ),
              editing && editing.jti === item.jti
                ? h(
                    "div",
                    { style: { display: "flex", alignItems: "center", gap: 8 } },
                    h(Input, {
                      size: "small",
                      value: editing.label,
                      maxLength: 64,
                      style: { width: 160 },
                      onChange: (event) =>
                        setEditing({ jti: item.jti, label: event.target.value }),
                    }),
                    h(
                      Button,
                      {
                        type: "primary",
                        size: "small",
                        onClick: () => rename(item.jti, editing.label.trim()),
                      },
                      "保存",
                    ),
                    h(Button, { size: "small", onClick: () => setEditing(null) }, "取消"),
                  )
                : item.revoked
                  ? h(
                      "div",
                      { style: { display: "flex", alignItems: "center", gap: 8 } },
                      h(Text, { type: "secondary", style: { fontSize: 12 } }, "已吊销"),
                      h(
                        Button,
                        { danger: true, size: "small", onClick: () => remove(item.jti) },
                        "删除",
                      ),
                    )
                  : h(
                      "div",
                      { style: { display: "flex", alignItems: "center", gap: 8 } },
                      h(
                        Button,
                        {
                          size: "small",
                          onClick: () => setEditing({ jti: item.jti, label: item.label || "" }),
                        },
                        "改名",
                      ),
                      h(
                        Button,
                        { danger: true, size: "small", onClick: () => revoke(item.jti) },
                        "吊销",
                      ),
                    ),
            ),
          ),
        )
      : null,
  );
}

/**
 * 插件设置页外壳。
 *
 * 菜单项只有一个（「QwenPaw Node Bridge」），页内按功能分块：目前只有 SSH 中继，
 * 后续要加检索、推送等设置时，在 BLOCKS 里追加一项即可，不用再注册路由。
 * `id` 用作锚点与折叠状态，改名会丢失用户已展开与否的偏好，因此保持稳定。
 */
const BLOCKS = [
  {
    id: "pairing",
    title: "设备配对",
    description: "签发手机端登录用的访问令牌，并显示可直接扫描的二维码。",
    render: () => h(PairingPanel),
  },
  {
    id: "ssh",
    title: "SSH 中继",
    description: "配置手机端「Bridge 中继」使用的 SSH 目标。",
    render: () => h(SshSettingsPanel),
  },
];

function BridgeSettingsPage() {
  const [openId, setOpenId] = React.useState(BLOCKS[0] ? BLOCKS[0].id : null);
  return h(
    "div",
    null,
    h(
      Paragraph,
      { type: "secondary", style: { marginBottom: 16 } },
      "来自 QwenPaw Node Bridge 的设置。每块设置独立保存，改动不会影响其它块。",
    ),
    h(
      Space,
      { direction: "vertical", size: 16, style: { width: "100%" } },
      ...BLOCKS.map((block) =>
        h(
          Card,
          {
            key: block.id,
            title: block.title,
            extra: h(
              Button,
              {
                type: "link",
                size: "small",
                onClick: () => setOpenId((current) => (current === block.id ? null : block.id)),
              },
              openId === block.id ? "收起" : "展开",
            ),
          },
          h(
            Text,
            { type: "secondary", style: { display: "block", marginBottom: openId === block.id ? 16 : 0 } },
            block.description,
          ),
          openId === block.id ? block.render() : null,
        ),
      ),
    ),
  );
}

if (!window.__qwenpawNodeBridgeSshRoute) {
  window.__qwenpawNodeBridgeSshRoute = true;
  window.QwenPaw.registerRoutes("qwenpaw-node-bridge", [
    {
      path: "/plugin/qwenpaw-node-bridge/ssh",
      label: "QwenPaw Node Bridge",
      // 2.3.0 的 registerRoutes 读顶层 title 生成路由 meta；2.2.2 读 meta.title。
      // 两处都给，跨版本都能显示正确的浏览器标题（缺省会回落成路径末段 “Ssh”）。
      title: "QwenPaw Node Bridge",
      meta: { title: "QwenPaw Node Bridge" },
      icon: h(BridgeIcon),
      component: BridgeSettingsPage,
      priority: 10,
    },
  ]);
}

export {};
