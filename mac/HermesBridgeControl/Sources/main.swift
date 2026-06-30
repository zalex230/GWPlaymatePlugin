import AppKit
import Foundation

struct LaunchService {
    let name: String
    let label: String
    let plistPath: String
}

final class CommandRunner {
    @discardableResult
    static func run(_ executable: String, _ arguments: [String]) -> (code: Int32, output: String) {
        let task = Process()
        task.executableURL = URL(fileURLWithPath: executable)
        task.arguments = arguments

        let pipe = Pipe()
        task.standardOutput = pipe
        task.standardError = pipe

        do {
            try task.run()
            task.waitUntilExit()
            let data = pipe.fileHandleForReading.readDataToEndOfFile()
            return (task.terminationStatus, String(data: data, encoding: .utf8) ?? "")
        } catch {
            return (127, error.localizedDescription)
        }
    }
}

final class LaunchAgentController {
    private let domain = "gui/\(getuid())"
    let services: [LaunchService]

    init(services: [LaunchService]) {
        self.services = services
    }

    func isLoaded(_ service: LaunchService) -> Bool {
        CommandRunner.run("/bin/launchctl", ["print", "\(domain)/\(service.label)"]).code == 0
    }

    func statusText() -> String {
        services.map { service in
            let loaded = isLoaded(service)
            return "\(service.name): \(loaded ? "Running" : "Stopped")"
        }.joined(separator: "\n")
    }

    func start(_ service: LaunchService) {
        if !isLoaded(service) {
            _ = CommandRunner.run("/bin/launchctl", ["bootstrap", domain, service.plistPath])
        }
        _ = CommandRunner.run("/bin/launchctl", ["kickstart", "-k", "\(domain)/\(service.label)"])
    }

    func stop(_ service: LaunchService) {
        _ = CommandRunner.run("/bin/launchctl", ["bootout", "\(domain)/\(service.label)"])
    }

    func restart(_ service: LaunchService) {
        if isLoaded(service) {
            _ = CommandRunner.run("/bin/launchctl", ["kickstart", "-k", "\(domain)/\(service.label)"])
        } else {
            start(service)
        }
    }

    func startBridge() {
        services.forEach(start)
    }

    func stopBridge() {
        services.reversed().forEach(stop)
    }

    func restartHermes() {
        if let hermes = services.first(where: { $0.label == "com.gwplaymate.hermes-daemon" }) {
            restart(hermes)
        }
    }
}

final class MainWindowController: NSWindowController {
    private let controller: LaunchAgentController
    private let statusLabel = NSTextField(labelWithString: "")

    init(controller: LaunchAgentController) {
        self.controller = controller

        let contentView = NSView(frame: NSRect(x: 0, y: 0, width: 420, height: 220))
        let window = NSWindow(
            contentRect: contentView.frame,
            styleMask: [.titled, .closable, .miniaturizable],
            backing: .buffered,
            defer: false
        )
        window.title = "Hermes Bridge Control"
        window.center()
        window.contentView = contentView

        super.init(window: window)
        buildInterface(in: contentView)
        refreshStatus()
    }

    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    private func buildInterface(in view: NSView) {
        let title = NSTextField(labelWithString: "Hermes Bridge")
        title.font = .systemFont(ofSize: 22, weight: .semibold)
        title.translatesAutoresizingMaskIntoConstraints = false

        let subtitle = NSTextField(labelWithString: "Open starts the bridge. Quit stops it. Restart recovers a stuck daemon.")
        subtitle.font = .systemFont(ofSize: 12)
        subtitle.textColor = .secondaryLabelColor
        subtitle.translatesAutoresizingMaskIntoConstraints = false

        statusLabel.font = .monospacedSystemFont(ofSize: 13, weight: .regular)
        statusLabel.lineBreakMode = .byWordWrapping
        statusLabel.translatesAutoresizingMaskIntoConstraints = false

        let startButton = button("Start", action: #selector(startBridge))
        let stopButton = button("Stop", action: #selector(stopBridge))
        let restartButton = button("Restart Hermes", action: #selector(restartHermes))
        let refreshButton = button("Refresh", action: #selector(refreshStatus))

        let buttons = NSStackView(views: [startButton, stopButton, restartButton, refreshButton])
        buttons.orientation = .horizontal
        buttons.spacing = 8
        buttons.translatesAutoresizingMaskIntoConstraints = false

        view.addSubview(title)
        view.addSubview(subtitle)
        view.addSubview(statusLabel)
        view.addSubview(buttons)

        NSLayoutConstraint.activate([
            title.topAnchor.constraint(equalTo: view.topAnchor, constant: 22),
            title.leadingAnchor.constraint(equalTo: view.leadingAnchor, constant: 24),
            title.trailingAnchor.constraint(equalTo: view.trailingAnchor, constant: -24),

            subtitle.topAnchor.constraint(equalTo: title.bottomAnchor, constant: 6),
            subtitle.leadingAnchor.constraint(equalTo: title.leadingAnchor),
            subtitle.trailingAnchor.constraint(equalTo: title.trailingAnchor),

            statusLabel.topAnchor.constraint(equalTo: subtitle.bottomAnchor, constant: 24),
            statusLabel.leadingAnchor.constraint(equalTo: title.leadingAnchor),
            statusLabel.trailingAnchor.constraint(equalTo: title.trailingAnchor),

            buttons.leadingAnchor.constraint(equalTo: title.leadingAnchor),
            buttons.trailingAnchor.constraint(lessThanOrEqualTo: title.trailingAnchor),
            buttons.bottomAnchor.constraint(equalTo: view.bottomAnchor, constant: -24)
        ])
    }

    private func button(_ title: String, action: Selector) -> NSButton {
        let button = NSButton(title: title, target: self, action: action)
        button.bezelStyle = .rounded
        button.translatesAutoresizingMaskIntoConstraints = false
        return button
    }

    @objc private func startBridge() {
        controller.startBridge()
        refreshStatus()
    }

    @objc private func stopBridge() {
        controller.stopBridge()
        refreshStatus()
    }

    @objc private func restartHermes() {
        controller.restartHermes()
        refreshStatus()
    }

    @objc private func refreshStatus() {
        statusLabel.stringValue = controller.statusText()
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    private let controller = LaunchAgentController(
        services: [
            LaunchService(
                name: "Hermes daemon",
                label: "com.gwplaymate.hermes-daemon",
                plistPath: "\(NSHomeDirectory())/Library/LaunchAgents/com.gwplaymate.hermes-daemon.plist"
            ),
            LaunchService(
                name: "Kokoro TTS",
                label: "com.gwplaymate.kokoro-fastapi",
                plistPath: "\(NSHomeDirectory())/Library/LaunchAgents/com.gwplaymate.kokoro-fastapi.plist"
            )
        ]
    )
    private var windowController: MainWindowController?

    func applicationDidFinishLaunching(_ notification: Notification) {
        controller.startBridge()
        let windowController = MainWindowController(controller: controller)
        self.windowController = windowController
        windowController.showWindow(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    func applicationWillTerminate(_ notification: Notification) {
        controller.stopBridge()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        true
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
