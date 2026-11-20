// Define main function (script entry)

function main(config, profileName) {
    if (config.proxies && Array.isArray(config.proxies)) {
        config.proxies.forEach((proxy) => {
            // 匹配你的 VLESS + Reality 节点
            if (proxy.type === "vless" && proxy["reality-opts"]) {
                // 1. 适配 Xray 26.9.30 Reality 默认的 X25519MLKEM768 密钥交换
                proxy["reality-opts"]["support-x25519mlkem768"] = true;

                // 2. 规范 xhttp 传输层参数（若节点走 xhttp/splithttp）
                if (proxy.network === "xhttp" || proxy.network === "splithttp") {
                    proxy.network = "xhttp";
                    proxy["xhttp-opts"] = Object.assign(
                        {
                            path: "/n1MQ",
                            mode: "auto",
                        },
                        proxy["xhttp-opts"] || {}
                    );
                }
            }
        });
    }
    return config;
}
