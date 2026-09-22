// 打开 GeckoView 的远程调试，让 Firefox RDP socket 出现。
//
// 用法（设备需 root，frida-server 已在跑）：
//   PID=$(adb -s <serial> shell "ps -A | grep '<pkg>$'" | awk '{print $2}')
//   frida -D <serial> -p $PID -l enable_remote_debugging.js --runtime=v8 -q \
//         --eval 'setTimeout(()=>{},2500)'
//   adb -s <serial> forward tcp:6080 localabstract:<pkg>/firefox-debugger-socket
//
// 为什么是 GeckoView 而不是 WebView —— 这一步排查花了最久：
//   * a11y 树把渲染节点标成 android.webkit.WebView
//   * @webview_devtools_remote_<pid> 这个 socket **确实存在**
//   * 但 Java.choose("android.webkit.WebView") 实例数 = 0
//   * CDP 的 /json/list 永远是 []
// 真相：真正画页面的是 org.mozilla.geckoview.GeckoView（枚举 View 子类才看到），
// 那个 webview socket 是个空壳。Gecko 只认 Firefox RDP。
//
// 另一个坑：setWebContentsDebuggingEnabled 必须在 UI 线程调用，
// 直接调会抛 "Toggling of Web Contents Debugging must be done on the UI thread"。
// GeckoRuntimeSettings.setRemoteDebuggingEnabled 没有这个限制，但保险起见也 post 到主线程。

Java.perform(function () {
  var out = [];
  var found = 0;

  Java.choose("org.mozilla.geckoview.GeckoRuntimeSettings", {
    onMatch: function (s) {
      found++;
      try {
        out.push("before remoteDebugging=" + s.getRemoteDebuggingEnabled());
        s.setRemoteDebuggingEnabled(true);
        out.push("after  remoteDebugging=" + s.getRemoteDebuggingEnabled());
      } catch (e) {
        out.push("set failed: " + e);
      }
    },
    onComplete: function () {
      out.push("GeckoRuntimeSettings instances: " + found);
      if (found === 0) {
        out.push("没找到 settings 实例 —— app 可能还没初始化 Gecko，先把页面打开再跑一次");
      }
      send(out.join("\n"));
    }
  });
});
