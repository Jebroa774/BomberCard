#include "web_portal.h"

#include <WiFi.h>
#include <esp_system.h>

#include "board_pins.h"
#include "firmware_config.h"
#include "json_util.h"
#include "web_ui.h"

namespace pocketlab {

WebPortal::WebPortal(HardwareManager &hardware, StorageManager &storage)
    : hardware_(hardware),
      storage_(storage),
      server_(config::HTTP_PORT),
      webSocket_(config::WEBSOCKET_PORT) {}

bool WebPortal::begin() {
  const uint64_t chipId = ESP.getEfuseMac();
  const uint32_t suffix = static_cast<uint32_t>(chipId & 0x00FFFFFFULL);
  const uint32_t passwordSeed = static_cast<uint32_t>((chipId >> 16) ^ chipId ^ 0xA73C91E5UL);
  char ssidBuffer[24];
  char passwordBuffer[16];
  snprintf(ssidBuffer, sizeof(ssidBuffer), "BomberCard-%06lX",
           static_cast<unsigned long>(suffix));
  snprintf(passwordBuffer, sizeof(passwordBuffer), "PL-%08lX",
           static_cast<unsigned long>(passwordSeed));
  ssid_ = ssidBuffer;
  password_ = passwordBuffer;

  char tokenBuffer[33];
  snprintf(tokenBuffer, sizeof(tokenBuffer), "%08lX%08lX%08lX%08lX",
           static_cast<unsigned long>(esp_random()),
           static_cast<unsigned long>(esp_random()),
           static_cast<unsigned long>(esp_random()),
           static_cast<unsigned long>(esp_random()));
  sessionToken_ = tokenBuffer;

  WiFi.mode(WIFI_AP);
  WiFi.softAPsetHostname(config::HOSTNAME);
  if (!WiFi.softAP(ssid_.c_str(), password_.c_str(), config::WIFI_CHANNEL, false,
                   config::WIFI_MAX_CLIENTS)) {
    return false;
  }

  configureRoutes();
  const char *trackedHeaders[] = {"X-BomberCard-Token"};
  server_.collectHeaders(trackedHeaders, 1);
  server_.begin();

  webSocket_.begin();
  webSocket_.enableHeartbeat(15000, 3000, 2);
  webSocket_.onEvent([this](uint8_t client, WStype_t type, uint8_t *payload,
                            size_t length) {
    if (type == WStype_CONNECTED) {
      String status = buildStatusJson();
      webSocket_.sendTXT(client, status);
    } else if (type == WStype_TEXT) {
      (void)payload;
      (void)length;
      String error = F("{\"type\":\"error\",\"error\":\"websocket_read_only\"}");
      webSocket_.sendTXT(client, error);
    }
  });
  return true;
}

void WebPortal::configureRoutes() {
  server_.on(F("/"), HTTP_GET, [this]() {
    server_.sendHeader(F("Cache-Control"), F("no-store"));
    server_.send_P(200, "text/html; charset=utf-8", WEB_UI);
  });
  server_.on(F("/healthz"), HTTP_GET,
             [this]() { sendJson(200, F("{\"ok\":true}")); });
  server_.on(F("/api/config"), HTTP_GET,
             [this]() { sendJson(200, buildConfigJson()); });
  server_.on(F("/api/status"), HTTP_GET,
             [this]() { sendJson(200, buildStatusJson()); });
  server_.on(F("/api/hardware"), HTTP_GET,
             [this]() { sendJson(200, hardware_.statusJson()); });
  server_.on(F("/api/gpio"), HTTP_GET,
             [this]() { sendJson(200, gpioStatusJson()); });
  server_.on(F("/api/files"), HTTP_GET, [this]() { handleFileList(); });
  server_.on(F("/api/file"), HTTP_GET, [this]() { handleFileDownload(); });
  server_.on(F("/api/file"), HTTP_DELETE, [this]() { handleFileDelete(); });
  server_.on(F("/api/upload"), HTTP_POST,
             [this]() { handleUploadComplete(); },
             [this]() { handleUploadChunk(); });
  server_.on(F("/api/lf/power"), HTTP_POST, [this]() { handleLfPower(); });
  server_.on(F("/api/lf/diagnose"), HTTP_POST, [this]() { handleLfDiagnose(); });
  server_.on(F("/api/security/pairing-window"), HTTP_POST,
             [this]() { handlePairingArm(); });
  server_.on(F("/api/sd/remount"), HTTP_POST, [this]() { handleSdRemount(); });
  server_.on(F("/api/subghz/tx"), HTTP_POST,
             [this]() { handleLockedTransmit(F("subghz_tx")); });
  server_.on(F("/api/ir/tx"), HTTP_POST,
             [this]() { handleIrTransmit(); });
  server_.on(F("/api/gpio/output"), HTTP_POST,
             [this]() { handleLockedTransmit(F("gpio_output")); });
  server_.onNotFound([this]() { sendError(404, F("not_found")); });
}

void WebPortal::poll() {
  server_.handleClient();
  webSocket_.loop();

  const uint32_t now = millis();
  if (now - lastBroadcastMs_ >= 1000) {
    lastBroadcastMs_ = now;
    String status = buildStatusJson();
    webSocket_.broadcastTXT(status);
  }
}

void WebPortal::sendJson(int code, const String &payload) {
  server_.sendHeader(F("Cache-Control"), F("no-store"));
  server_.send(code, F("application/json; charset=utf-8"), payload);
}

void WebPortal::sendError(int code, const __FlashStringHelper *error) {
  String payload = F("{\"ok\":false,\"error\":\"");
  payload += error;
  payload += F("\"}");
  sendJson(code, payload);
}

bool WebPortal::authorizeMutation() {
  if (server_.hasHeader(F("X-BomberCard-Token")) &&
      server_.header(F("X-BomberCard-Token")) == sessionToken_) {
    return true;
  }
  sendError(403, F("invalid_session_token"));
  return false;
}

String WebPortal::buildStatusJson() const {
  String result;
  result.reserve(1400);
  result += F("{\"type\":\"status\",\"firmware\":{\"name\":\"");
  result += config::FIRMWARE_NAME;
  result += F("\",\"version\":\"");
  result += config::FIRMWARE_VERSION;
  result += F("\",\"hardwareRevision\":");
  result += POCKETLAB_CARD_HW_REV;
  result += F("},\"uptimeMs\":");
  result += millis();
  result += F(",\"freeHeap\":");
  result += ESP.getFreeHeap();
  result += F(",\"wifi\":{\"ssid\":\"");
  result += json::escape(ssid_);
  result += F("\",\"ip\":\"");
  result += WiFi.softAPIP().toString();
  result += F("\",\"clients\":");
  result += WiFi.softAPgetStationNum();
  result += F("},\"hardware\":");
  result += hardware_.statusJson();
  result += F(",\"storage\":");
  result += storage_.statusJson();
  result += '}';
  return result;
}

String WebPortal::buildConfigJson() const {
  String result;
  result.reserve(320);
  result += F("{\"token\":\"");
  result += sessionToken_;
  result += F("\",\"websocketPort\":");
  result += config::WEBSOCKET_PORT;
  result += F(",\"ssid\":\"");
  result += json::escape(ssid_);
  result += F("\",\"txPolicy\":{\"subGhz\":");
  result += json::boolean(config::SUBGHZ_TX_COMPILED);
  result += F(",\"ir\":");
  result += json::boolean(config::IR_TX_COMPILED);
  result += F(",\"gpioOutput\":");
  result += json::boolean(config::GPIO_OUTPUT_COMPILED);
  result += F("}}");
  return result;
}

String WebPortal::gpioStatusJson() const {
  String result = F("{\"mode\":\"input_only\",\"pins\":[");
  size_t index = 0;
  for (const uint8_t gpio : pins::DIRECT_EXPANSION) {
    bool level = false;
    hardware_.readDirectGpio(gpio, level);
    if (index++ > 0) result += ',';
    result += F("{\"gpio\":");
    result += static_cast<unsigned>(gpio);
    result += F(",\"level\":");
    result += json::boolean(level);
    result += '}';
  }
  result += F("],\"expanded\":{\"present\":");
  result += json::boolean(hardware_.status().statusExpanderPresent);
  result += F(",\"pins\":[");
  for (uint8_t bit = 0; bit < 8; ++bit) {
    if (bit > 0) result += ',';
    result += F("{\"name\":\"EX");
    result += static_cast<unsigned>(bit);
    result += F("\",\"level\":");
    result += json::boolean((hardware_.status().expansionInputs & (1U << bit)) != 0);
    result += '}';
  }
  result += F("]}}");
  return result;
}

void WebPortal::handleFileList() {
  if (!storage_.available()) {
    sendError(503, F("sd_not_mounted"));
    return;
  }
  String path;
  if (!storage_.normalizePath(server_.arg(F("path")), path)) {
    sendError(400, F("invalid_path"));
    return;
  }
  const String payload = storage_.listJson(path);
  sendJson(payload.indexOf(F("\"ok\":true")) >= 0 ? 200 : 404, payload);
}

String WebPortal::contentType(const String &path) const {
  if (path.endsWith(F(".csv"))) return F("text/csv");
  if (path.endsWith(F(".gpx"))) return F("application/gpx+xml");
  if (path.endsWith(F(".json"))) return F("application/json");
  if (path.endsWith(F(".txt")) || path.endsWith(F(".log"))) return F("text/plain");
  return F("application/octet-stream");
}

void WebPortal::handleFileDownload() {
  String path;
  if (!storage_.normalizePath(server_.arg(F("path")), path, false)) {
    sendError(400, F("invalid_path"));
    return;
  }
  File file = storage_.openRead(path);
  if (!file || file.isDirectory()) {
    if (file) file.close();
    sendError(404, F("file_not_found"));
    return;
  }
  const int slash = path.lastIndexOf('/');
  const String filename = slash >= 0 ? path.substring(slash + 1) : path;
  server_.sendHeader(F("Content-Disposition"), String(F("attachment; filename=\"")) + filename + '"');
  server_.streamFile(file, contentType(path));
  file.close();
}

void WebPortal::handleFileDelete() {
  if (!authorizeMutation()) return;
  String path;
  if (!storage_.normalizePath(server_.arg(F("path")), path, false)) {
    sendError(400, F("invalid_path"));
    return;
  }
  if (!storage_.removePath(path)) {
    sendError(409, F("delete_failed"));
    return;
  }
  sendJson(200, F("{\"ok\":true,\"message\":\"deleted\"}"));
}

void WebPortal::handleUploadChunk() {
  HTTPUpload &upload = server_.upload();
  if (upload.status == UPLOAD_FILE_START) {
    uploadAuthorized_ = server_.hasHeader(F("X-BomberCard-Token")) &&
                        server_.header(F("X-BomberCard-Token")) == sessionToken_;
    uploadSucceeded_ = false;
    uploadError_ = String();
    if (!uploadAuthorized_) {
      uploadError_ = F("invalid_session_token");
      return;
    }

    String requested = server_.arg(F("path"));
    if (requested.isEmpty()) {
      String filename = upload.filename;
      filename.replace('\\', '/');
      const int slash = filename.lastIndexOf('/');
      if (slash >= 0) filename = filename.substring(slash + 1);
      requested = F("/uploads/");
      requested += filename;
    }
    String path;
    if (!storage_.normalizePath(requested, path, false)) {
      uploadError_ = F("invalid_path");
      return;
    }
    uploadSucceeded_ = storage_.beginUpload(path);
    if (!uploadSucceeded_) uploadError_ = F("upload_open_failed");
  } else if (upload.status == UPLOAD_FILE_WRITE) {
    if (uploadSucceeded_ && !storage_.writeUpload(upload.buf, upload.currentSize)) {
      uploadSucceeded_ = false;
      uploadError_ = F("upload_write_failed");
    }
  } else if (upload.status == UPLOAD_FILE_END) {
    if (storage_.uploadActive()) storage_.finishUpload(uploadSucceeded_);
  } else if (upload.status == UPLOAD_FILE_ABORTED) {
    uploadSucceeded_ = false;
    uploadError_ = F("upload_aborted");
    if (storage_.uploadActive()) storage_.finishUpload(false);
  }
}

void WebPortal::handleUploadComplete() {
  if (!uploadAuthorized_) {
    sendError(403, F("invalid_session_token"));
  } else if (!uploadSucceeded_) {
    String payload = F("{\"ok\":false,\"error\":\"");
    payload += json::escape(uploadError_.isEmpty() ? String(F("upload_failed")) : uploadError_);
    payload += F("\"}");
    sendJson(409, payload);
  } else {
    sendJson(200, F("{\"ok\":true,\"message\":\"uploaded\"}"));
  }
}

void WebPortal::handleLfPower() {
  if (!authorizeMutation()) return;
  const bool enabled = server_.arg(F("enabled")) == F("1") ||
                       server_.arg(F("enabled")) == F("true");
  if (!hardware_.setLfRfidPower(enabled)) {
    sendError(503, enabled ? F("lf_rfid_self_test_failed")
                           : F("control_expander_unavailable"));
    return;
  }
  sendJson(200, F("{\"ok\":true,\"message\":\"lf_rfid_power_changed\"}"));
}

void WebPortal::handleLfDiagnose() {
  if (!authorizeMutation()) return;
  if (!hardware_.status().lfRfidPowerEnabled) {
    sendError(409, F("lf_rfid_power_off"));
    return;
  }
  if (!hardware_.diagnoseLfRfid()) {
    sendError(503, F("lf_rfid_self_test_failed"));
    return;
  }
  sendJson(200, F("{\"ok\":true,\"message\":\"lf_rfid_diagnosis_complete\"}"));
}

void WebPortal::handlePairingArm() {
  if (!authorizeMutation()) return;
  if (!hardware_.armPairingWindow()) {
    sendError(409, F("hold_pair_button_and_check_secure_element"));
    return;
  }
  sendJson(200, F("{\"ok\":true,\"message\":\"pairing_window_open_60s\"}"));
}

void WebPortal::handleSdRemount() {
  if (!authorizeMutation()) return;
  if (!storage_.remount()) {
    sendError(503, F("sd_mount_failed"));
    return;
  }
  sendJson(200, F("{\"ok\":true,\"message\":\"sd_mounted\"}"));
}

void WebPortal::handleIrTransmit() {
  if (!authorizeMutation()) return;
  if (!config::IR_TX_COMPILED) {
    String payload = F("{\"ok\":false,\"error\":\"capability_locked\",\"capability\":\"ir_tx\"}");
    sendJson(403, payload);
    return;
  }
  if (!server_.hasArg(F("address")) || !server_.hasArg(F("command"))) {
    sendError(400, F("address_and_command_required"));
    return;
  }

  const String addressText = server_.arg(F("address"));
  const String commandText = server_.arg(F("command"));
  const String repeatsText = server_.hasArg(F("repeats"))
                                 ? server_.arg(F("repeats"))
                                 : String(F("0"));
  char *end = nullptr;
  const long address = strtol(addressText.c_str(), &end, 0);
  if (end == addressText.c_str() || *end != '\0' || address < 0 || address > 255) {
    sendError(400, F("invalid_address"));
    return;
  }
  const long command = strtol(commandText.c_str(), &end, 0);
  if (end == commandText.c_str() || *end != '\0' || command < 0 || command > 255) {
    sendError(400, F("invalid_command"));
    return;
  }
  const long repeats = strtol(repeatsText.c_str(), &end, 0);
  if (end == repeatsText.c_str() || *end != '\0' || repeats < 0 || repeats > 2) {
    sendError(400, F("invalid_repeats"));
    return;
  }
  if (!hardware_.sendIrNec(static_cast<uint8_t>(address),
                           static_cast<uint8_t>(command),
                           static_cast<uint8_t>(repeats))) {
    sendError(429, F("ir_busy_or_power_unavailable"));
    return;
  }
  sendJson(200, F("{\"ok\":true,\"message\":\"ir_nec_sent\"}"));
}

void WebPortal::handleLockedTransmit(const __FlashStringHelper *capability) {
  if (!authorizeMutation()) return;
  String payload = F("{\"ok\":false,\"error\":\"capability_locked\",\"capability\":\"");
  payload += capability;
  payload += F("\"}");
  sendJson(403, payload);
}

}  // namespace pocketlab
