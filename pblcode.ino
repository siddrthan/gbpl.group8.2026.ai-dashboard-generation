#include <DHT.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include "esp_wpa2.h"

#define DHT_PIN     4
#define DHT_TYPE    DHT11
#define LDR1_PIN    34 //up
#define LDR2_PIN    35 //left
#define LDR3_PIN    32 //right
#define BTN_PIN     15

DHT dht(DHT_PIN, DHT_TYPE);

const char* ssid         = "wifi name";
const char* eap_identity = "ip address";
const char* eap_username = "ip address";
const char* eap_password = "your password";

const char* scriptURL = "https://script.google.com/your url";

bool collecting          = false;
int  readingCount        = 0;
const int TOTAL_READINGS = 15;

void connectWiFi() {
  WiFi.disconnect(true);
  WiFi.mode(WIFI_STA);

  esp_wifi_sta_wpa2_ent_set_identity((uint8_t*)eap_identity, strlen(eap_identity));
  esp_wifi_sta_wpa2_ent_set_username((uint8_t*)eap_username, strlen(eap_username));
  esp_wifi_sta_wpa2_ent_set_password((uint8_t*)eap_password, strlen(eap_password));
  esp_wifi_sta_wpa2_ent_enable();

  WiFi.begin(ssid);

  Serial.print("Connecting to ");
  Serial.println(ssid);

  int attempts = 0;
  while (WiFi.status() != WL_CONNECTED && attempts < 40) {
    delay(500);
    Serial.print(".");
    Serial.print(WiFi.status());
    attempts++;
  }

  if (WiFi.status() == WL_CONNECTED) {
    Serial.println("\nWiFi connected! IP: ");
    Serial.println(WiFi.localIP());
  } else {
    Serial.println("\nFailed to connect. Will retry in loop.");
  }
}

void setup() {
  Serial.begin(115200);
  pinMode(BTN_PIN, INPUT_PULLUP);
  dht.begin();
  Serial.println("Ready. Press button to start collecting.");
}

void loop() {
  if (digitalRead(BTN_PIN) == LOW) {
    delay(300);

    if (!collecting) {
      collecting   = true;
      readingCount = 0;
      Serial.println("Button pressed — connecting to WiFi...");
      connectWiFi();
      Serial.println("Started collecting. Taking 15 readings...");
    }
  }

  if (!collecting) return;

  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("WiFi lost, reconnecting...");
    connectWiFi();
  }

  // Wait 10s between readings
  for (int i = 0; i < 100; i++) {
    delay(100);
  }

  float humidity    = dht.readHumidity();
  float temperature = dht.readTemperature();

  int ldr1Raw  = analogRead(LDR1_PIN);
  int ldr2Raw  = analogRead(LDR2_PIN);
  int ldr3Raw  = analogRead(LDR3_PIN);

  int light1Pct = map(ldr1Raw, 0, 4095, 0, 100);
  int light2Pct = map(ldr2Raw, 0, 4095, 0, 100);
  int light3Pct = map(ldr3Raw, 0, 4095, 0, 100);

  if (isnan(humidity) || isnan(temperature)) {
    Serial.println("DHT read failed! (still counts toward total)");
    readingCount++;
  } else if (WiFi.status() == WL_CONNECTED) {
    HTTPClient http;

    String url = String(scriptURL) +
                 "?temp="    + String(temperature) +
                 "&humidity="+ String(humidity) +
                 "&light1="  + String(light1Pct) +
                 "&light2="  + String(light2Pct) +
                 "&light3="  + String(light3Pct);

    http.begin(url);
    http.setFollowRedirects(HTTPC_STRICT_FOLLOW_REDIRECTS);
    int httpCode = http.GET();

    Serial.print("Reading ");
    Serial.print(readingCount + 1);
    Serial.print("/15 | T:");
    Serial.print(temperature);
    Serial.print(" H:");
    Serial.print(humidity);
    Serial.print(" L1:");
    Serial.print(light1Pct);
    Serial.print(" L2:");
    Serial.print(light2Pct);
    Serial.print(" L3:");
    Serial.print(light3Pct);
    Serial.print(" | HTTP: ");
    Serial.println(httpCode);
    http.end();

    readingCount++;
  }

  if (readingCount >= TOTAL_READINGS) {
    collecting = false;
    WiFi.disconnect(true);
    Serial.println("✅ 15 readings complete. Press button to start again.");
  }
}
