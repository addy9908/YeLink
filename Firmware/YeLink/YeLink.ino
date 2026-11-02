/*
 * DIY IO Box for EthoVision - Arduino Uno R3
 * Receives:  p=<pin> v=<0|1> d=<seconds>  (space-separated, newline-terminated)
 *   d=0  -> latch (set and hold)
 *   d>0  -> set, then revert to opposite after duration
 */
const unsigned long BAUD = 115200;   // match MyAdapter.exe
const int MAX_PINS = 14;

bool     timerActive[MAX_PINS];
unsigned long timerEnd[MAX_PINS];
int      revertValue[MAX_PINS];
String   buf = "";

void setup() {
  Serial.begin(BAUD);
  for (int i = 2; i <= 13; i++) { pinMode(i, OUTPUT); digitalWrite(i, LOW); }
  for (int i = 0; i < MAX_PINS; i++) { timerActive[i]=false; timerEnd[i]=0; revertValue[i]=LOW; }
  Serial.println("READY");
}

void loop() {
  while (Serial.available() > 0) {
    char c = (char)Serial.read();
    if (c=='\n' || c=='\r' || c==';') { if (buf.length()>0){ processCommand(buf); buf=""; } }
    else buf += c;
  }
  unsigned long now = millis();
  for (int i = 2; i <= 13; i++) {
    if (timerActive[i] && (long)(now - timerEnd[i]) >= 0) {
      digitalWrite(i, revertValue[i]);
      timerActive[i] = false;
      Serial.print("REVERT p="); Serial.print(i);
      Serial.print(" v="); Serial.println(revertValue[i]);
    }
  }
}

void processCommand(String cmd) {
  cmd.trim(); if (cmd.length()==0) return;
  if (cmd.equalsIgnoreCase("PING")) { Serial.println("PONG"); return; }
  int pin=-1, val=0; float dur=0.0; bool havePin=false;
  cmd.replace(",", " "); cmd.replace(";", " ");
  int start=0;
  while (start < (int)cmd.length()) {
    int sp = cmd.indexOf(' ', start); if (sp==-1) sp=cmd.length();
    String tok = cmd.substring(start, sp); tok.trim();
    if (tok.length()>=2 && tok.charAt(1)=='=') {
      char key=tok.charAt(0); String value=tok.substring(2);
      if (key=='p'){ pin=value.toInt(); havePin=true; }
      else if (key=='v'){ val=(value.toInt()!=0)?HIGH:LOW; }
      else if (key=='d'){ dur=value.toFloat(); }
    }
    start = sp+1;
  }
  if (!havePin || pin<2 || pin>13) { Serial.print("ERR bad pin: "); Serial.println(pin); return; }
  digitalWrite(pin, val);
  if (val == HIGH && dur > 0.0) {
    // Timed ON: go HIGH now, revert to LOW after duration
    timerEnd[pin]=millis()+(unsigned long)(dur*1000.0);
    timerActive[pin]=true;
    revertValue[pin]=LOW;
  } else {
    // v=0 (off) OR v=1 with d=0 (latched on): ignore/cancel any timer
    timerActive[pin]=false;
  }
  Serial.print("OK p="); Serial.print(pin);
  Serial.print(" v="); Serial.print(val);
  Serial.print(" d="); Serial.println(dur,2);
}