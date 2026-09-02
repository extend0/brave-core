// Copyright (c) 2026 The Brave Authors. All rights reserved.
// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this file,
// You can obtain one at https://mozilla.org/MPL/2.0/.

import Foundation
import Web
import WebKit

/// Page-world fixes for SoundCloud's mobile web app: registers Media Session
/// `nexttrack`/`previoustrack` handlers so lock screen and headphone controls
/// change tracks instead of seeking 15 seconds, and adds an ordered "Play"
/// button to playlist pages, which otherwise only offer "Shuffle play".
/// The script never posts messages to native.
class SoundCloudScriptHandler: TabContentScript {
  static let scriptName = "SoundCloudScript"
  static let scriptId = UUID().uuidString
  static let messageHandlerName = "\(scriptName)_\(messageUUID)"
  static let scriptSandbox: WKContentWorld = .page
  static let userScript: WKUserScript? = {
    guard let script = loadUserScript(named: scriptName) else {
      return nil
    }

    return WKUserScript(
      source: secureScript(
        handlerNamesMap: [:],
        securityToken: scriptId,
        script: script
      ),
      injectionTime: .atDocumentStart,
      forMainFrameOnly: true,
      in: scriptSandbox
    )
  }()

  func tab(
    _ tab: some TabState,
    receivedScriptMessage message: WKScriptMessage,
    replyHandler: @escaping (Any?, String?) -> Void
  ) {
    // This script does not send messages.
    replyHandler(nil, nil)
  }
}
