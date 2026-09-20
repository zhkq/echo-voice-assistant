import AppKit
import WebKit
import Darwin

// The host only exposes three window commands to pages from this ECHO instance.
final class EchoPanel: NSPanel {
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { false }
}

final class EdgeView: NSView {
    var onEnter: (() -> Void)?
    override func updateTrackingAreas() {
        super.updateTrackingAreas()
        trackingAreas.forEach { removeTrackingArea($0) }
        addTrackingArea(NSTrackingArea(rect: bounds,
            options: [.mouseEnteredAndExited, .activeAlways, .inVisibleRect], owner: self))
    }
    override func mouseEntered(with event: NSEvent) { onEnter?() }
    override func mouseDown(with event: NSEvent) { onEnter?() }
}

final class Sidebar: NSObject, NSApplicationDelegate, WKScriptMessageHandler,
                     WKNavigationDelegate, WKUIDelegate {
    var port: Int
    let command: String
    let dataDir: String?
    var baseURL: URL { URL(string: "http://127.0.0.1:\(port)")! }
    var commandFile: URL!
    var lastCommand = ""
    var commandTimer: Timer?
    var lockFD: Int32 = -1
    var panel: EchoPanel!
    var rail: WKWebView!
    var dashboard: WKWebView!
    var edge: EdgeView!
    var menuItem: NSStatusItem!
    var state = "collapsed"
    var selectedScreen: NSScreen?
    var retryTimer: Timer?
    var hoverTimer: Timer?
    var outsideSince: TimeInterval?
    var hoverArmed = true
    var failedViews = Set<ObjectIdentifier>()

    init(port: Int, command: String, data: String?) {
        self.port = port; self.command = command; self.dataDir = data
    }

    // ECHO 的实际端口以 echo-port.txt 为权威来源。首选端口被占/落在保留段时 ECHO 会让位，
    // 只按启动参数里的端口加载就会一直连不上（已渲染的页面点按钮报 "Load failed"）。
    func resolvedPort() -> Int {
        guard let dir = dataDir else { return port }
        let file = URL(fileURLWithPath: dir).appendingPathComponent("echo-port.txt")
        guard let text = try? String(contentsOf: file, encoding: .utf8),
              let value = Int(text.trimmingCharacters(in: .whitespacesAndNewlines)),
              (1...65535).contains(value) else { return port }
        return value
    }

    // 重试前对齐端口；变了就整页重载 —— origin 都换了，只重连旧端口没用。
    func refreshPort() {
        let fresh = resolvedPort()
        guard fresh != port else { return }
        port = fresh
        NSLog("ECHO sidebar: port -> %d", port)
        rail?.load(URLRequest(url: baseURL.appendingPathComponent("/web/rail.html")))
        dashboard?.load(URLRequest(url: baseURL))
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        let dir = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/ECHO")
        do { try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true) }
        catch { NSLog("ECHO sidebar: %@", error.localizedDescription); NSApp.terminate(nil); return }
        commandFile = dir.appendingPathComponent("sidebar-\(port).command")
        lockFD = Darwin.open(dir.appendingPathComponent("sidebar-\(port).lock").path,
                             O_CREAT | O_RDWR | O_NOFOLLOW, S_IRUSR | S_IWUSR)
        guard lockFD >= 0 else { NSApp.terminate(nil); return }
        if flock(lockFD, LOCK_EX | LOCK_NB) != 0 {
            // Autostart must never toggle an already open window on server restart.
            if ["toggle", "expand", "quit"].contains(command) {
                do {
                    try "\(UUID().uuidString)\n\(command)".write(to: commandFile,
                        atomically: true, encoding: .utf8)
                } catch { NSLog("ECHO sidebar command: %@", error.localizedDescription) }
            }
            NSApp.terminate(nil)
            return
        }
        if command == "quit" { NSApp.terminate(nil); return }
        // A local mailbox also works when launched by Python without a LaunchServices
        // application session. Each UUID makes repeated identical commands distinct.
        lastCommand = (try? String(contentsOf: commandFile, encoding: .utf8)) ?? ""
        commandTimer = Timer.scheduledTimer(withTimeInterval: 0.15, repeats: true) { [weak self] _ in
            guard let self = self,
                  let text = try? String(contentsOf: self.commandFile, encoding: .utf8),
                  text != self.lastCommand else { return }
            self.lastCommand = text
            self.receiveCommand(text.components(separatedBy: "\n").last ?? "")
        }
        selectedScreen = screenAtMouse()
        panel = EchoPanel(contentRect: .zero,
            styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        panel.title = "ECHO 浮动面板"
        panel.level = .floating
        panel.hidesOnDeactivate = false
        panel.isReleasedWhenClosed = false
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        panel.backgroundColor = NSColor(red: 0.09, green: 0.10, blue: 0.14, alpha: 1)
        panel.hasShadow = true
        rail = makeWebView(path: "/web/rail.html")
        dashboard = makeWebView(path: "/")
        edge = EdgeView(frame: .zero)
        edge.wantsLayer = true
        edge.layer?.backgroundColor = NSColor.systemBlue.cgColor
        edge.onEnter = { [weak self] in
            guard let self = self, self.hoverArmed else { return }
            self.show("expanded", activate: false)
        }
        menuItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        menuItem.button?.title = "ECHO"
        let menu = NSMenu()
        for (title, action) in [("展开 / 收起", #selector(toggle)),
                                ("移到当前屏幕", #selector(moveToMouse)),
                                ("隐藏到右边缘", #selector(hideToEdge)),
                                ("在浏览器打开", #selector(openBrowser)),
                                ("退出浮动框", #selector(quit))] {
            let item = NSMenuItem(title: title, action: action, keyEquivalent: "")
            item.target = self
            menu.addItem(item)
        }
        menuItem.menu = menu
        NotificationCenter.default.addObserver(self, selector: #selector(screenChanged),
            name: NSApplication.didChangeScreenParametersNotification, object: nil)
        // Automatic startup leaves only the edge; explicit open commands still open the panel.
        show(command == "collapsed" ? "hidden" : "expanded")
        let timer = Timer(timeInterval: 0.1, repeats: true) { [weak self] _ in
            self?.updateHover()
        }
        hoverTimer = timer
        RunLoop.main.add(timer, forMode: .common)
        retryTimer = Timer.scheduledTimer(withTimeInterval: 5, repeats: true) { [weak self] _ in
            guard let self = self else { return }
            self.refreshPort()
            for view in [self.rail, self.dashboard].compactMap({ $0 })
                where !view.isLoading && (view.url == nil || self.failedViews.contains(ObjectIdentifier(view))) {
                let path = view === self.rail ? "/web/rail.html" : "/"
                view.load(URLRequest(url: self.baseURL.appendingPathComponent(path)))
            }
        }
    }

    func makeWebView(path: String) -> WKWebView {
        let config = WKWebViewConfiguration()
        let controller = config.userContentController
        controller.add(self, name: "echoSidebar")
        // Reuse the existing Windows frontend bridge without changing shared web assets.
        controller.addUserScript(WKUserScript(source: """
            window.chrome = window.chrome || {};
            window.chrome.webview = {postMessage: function(message) {
                window.webkit.messageHandlers.echoSidebar.postMessage(message);
            }};
            document.addEventListener('DOMContentLoaded', function() {
                const hint = document.querySelector('.hint');
                if (location.pathname === '/web/rail.html' && hint) {
                    hint.textContent = '菜单栏 ECHO';
                }
                ['btnExpand', 'btnRailCollapse'].forEach(function(id) {
                    const button = document.getElementById(id);
                    if (button) button.title = id === 'btnExpand'
                        ? '展开仪表盘（也可使用菜单栏 ECHO）'
                        : '收起为折叠条（也可使用菜单栏 ECHO）';
                });
            });
            """, injectionTime: .atDocumentStart, forMainFrameOnly: true))
        let view = WKWebView(frame: .zero, configuration: config)
        view.navigationDelegate = self
        view.uiDelegate = self
        view.autoresizingMask = [.width, .height]
        view.load(URLRequest(url: baseURL.appendingPathComponent(path)))
        return view
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!,
                 withError error: Error) {
        if (error as NSError).code != NSURLErrorCancelled {
            failedViews.insert(ObjectIdentifier(webView))
        }
    }
    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        failedViews.remove(ObjectIdentifier(webView))
    }
    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
        failedViews.insert(ObjectIdentifier(webView))
    }

    func isLocal(_ url: URL) -> Bool {
        url.scheme == "http" && url.host == "127.0.0.1" && url.port == port
    }

    func userContentController(_ userContentController: WKUserContentController,
                               didReceive message: WKScriptMessage) {
        let origin = message.frameInfo.securityOrigin
        guard message.frameInfo.isMainFrame, origin.protocol == "http",
              origin.host == "127.0.0.1", origin.port == port,
              let text = message.body as? String else { return }
        switch text {
        case "rail-expand": show("expanded")
        case "rail-collapse": show("collapsed")
        case "rail-hide": show("hidden")
        default: break
        }
    }

    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = action.request.url else { decisionHandler(.cancel); return }
        if isLocal(url) {
            if action.targetFrame == nil {
                NSWorkspace.shared.open(url)
                decisionHandler(.cancel)
            } else { decisionHandler(.allow) }
        } else {
            if action.navigationType == .linkActivated && ["http", "https"].contains(url.scheme ?? "") {
                NSWorkspace.shared.open(url)
            }
            decisionHandler(.cancel)
        }
    }

    // window.open(...) / target=_blank 走这里 —— WKWebView 不会为它触发 decidePolicyFor。
    // Windows 边条用 WebView2 的 NewWindowRequested 在默认浏览器打开（sidebar/Program.cs），
    // mac 用 NSWorkspace 对齐；否则面板里点会议详情
    //（window.open('/web/meeting.html?id=...')）会毫无反应。
    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for navigationAction: WKNavigationAction,
                 windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = navigationAction.request.url,
           ["http", "https"].contains(url.scheme ?? "") {
            NSWorkspace.shared.open(url)
        }
        return nil
    }

    func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
        let alert = NSAlert()
        alert.messageText = message
        alert.addButton(withTitle: "确定")
        alert.addButton(withTitle: "取消")
        completionHandler(alert.runModal() == .alertFirstButtonReturn)
    }

    func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
        let alert = NSAlert()
        alert.messageText = message
        alert.runModal()
        completionHandler()
    }

    func screenAtMouse() -> NSScreen? {
        NSScreen.screens.first(where: { $0.frame.contains(NSEvent.mouseLocation) }) ?? NSScreen.main
    }

    func show(_ newState: String, activate: Bool = true) {
        state = newState
        let content: NSView = state == "expanded" ? dashboard : (state == "hidden" ? edge : rail)
        panel.contentView = content
        position()
        outsideSince = ProcessInfo.processInfo.systemUptime
        if state == "hidden" {
            // Manual hide while the pointer is still on the edge must not immediately reopen.
            hoverArmed = !panel.frame.contains(NSEvent.mouseLocation)
        }
        panel.orderFrontRegardless()
        if state == "expanded" && activate { panel.makeKey() }
    }

    func updateHover() {
        let inside = panel.frame.contains(NSEvent.mouseLocation)
        if state == "hidden" {
            if !inside { hoverArmed = true }
            if inside && hoverArmed { show("expanded", activate: false) }
            return
        }
        // Do not retract during a drag, selection, native dialog or popup menu.
        if inside || NSEvent.pressedMouseButtons != 0 || NSApp.modalWindow != nil
            || panel.attachedSheet != nil || NSApp.currentEvent?.type == .leftMouseDragged {
            outsideSince = nil
            return
        }
        let now = ProcessInfo.processInfo.systemUptime
        if let since = outsideSince {
            if now - since >= 0.6 { show("hidden", activate: false) }
        } else {
            outsideSince = now
        }
    }

    func position() {
        guard let screen = selectedScreen ?? NSScreen.main else { return }
        let area = screen.visibleFrame
        let width: CGFloat = state == "expanded" ? min(450, area.width) : (state == "hidden" ? 4 : 64)
        panel.setFrame(NSRect(x: area.maxX - width, y: area.minY, width: width, height: area.height), display: true)
        panel.contentView?.frame = NSRect(x: 0, y: 0, width: width, height: area.height)
    }

    @objc func screenChanged() {
        selectedScreen = NSScreen.screens.first(where: { $0.deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")] as? NSNumber == selectedScreen?.deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")] as? NSNumber }) ?? NSScreen.main
        position()
    }
    @objc func toggle() { show(state == "expanded" ? "collapsed" : "expanded") }
    @objc func moveToMouse() { selectedScreen = screenAtMouse(); position(); panel.orderFrontRegardless() }
    @objc func hideToEdge() { show("hidden") }
    @objc func openBrowser() { NSWorkspace.shared.open(baseURL) }
    @objc func quit() { NSApp.terminate(nil) }
    func receiveCommand(_ command: String) {
        switch command {
        case "toggle": toggle()
        case "expand": show("expanded")
        case "quit": quit()
        default: break
        }
    }
}

let args = CommandLine.arguments
func value(after key: String) -> String? {
    guard let i = args.firstIndex(of: key), i + 1 < args.count else { return nil }
    return args[i + 1]
}
guard let port = Int(value(after: "--port") ?? "8970"), (1...65535).contains(port) else {
    fputs("Invalid port\n", stderr)
    exit(2)
}
let command = value(after: "--command") ?? "toggle"
guard ["toggle", "expand", "expanded", "collapsed", "quit"].contains(command) else { exit(2) }
let dataDir = value(after: "--data")
let delegate = Sidebar(port: port, command: command, data: dataDir)
let application = NSApplication.shared
application.setActivationPolicy(.accessory)
application.delegate = delegate
application.run()
