/*
 * YeLink_EdgeLogger_Teensy_v1.ino  --  Teensy 4.0 edge logger for YeLink Test 2/3
 * (original version: uses Teensy-only digitalReadFast() and Serial.send_now();
 *  compiles ONLY with Tools > Board = Teensy 4.0)
 *
 * Records every rising and falling edge on the monitored pins with a micros()
 * timestamp taken inside the interrupt, and streams them over USB serial.
 * Also answers a clock-sync query so the PC can map Teensy time to PC time.
 *
 * WIRING (Teensy 4.0 pins are 3.3 V, NOT 5 V tolerant; the Uno outputs 5 V):
 *   Uno pin N  ---[ 10 k ]---+--- Teensy pin N
 *                            |
 *                          [ 20 k ]
 *                            |
 *                           GND          (gives 3.33 V at the Teensy pin)
 *   Uno GND --- Teensy GND  (common ground is required)
 *   Default monitored pins: Uno 2,3,4,5 -> Teensy 2,3,4,5 (same numbers for clarity).
 *
 * SERIAL PROTOCOL (115200, USB speed is independent of the baud value):
 *   -> "READY,YeLink_EdgeLogger,pins=2,3,4,5"      at start
 *   -> "E,<micros>,<pin>,<state>"                    one line per edge, state 1 = rising, 0 = falling
 *   <- 'T'   (one byte)   -> "S,<micros>"            clock-sync reply, sent immediately
 *   <- 'Q'   (one byte)   -> "P,<micros>,<pin>,<state>" for every pin: current levels
 *   -> "D,<count>"                                   only if the edge queue overflowed (should never appear)
 *
 * Board: Teensy 4.0, USB Type: Serial, CPU speed: default.
 */

const uint8_t PINS[]  = {2, 3, 4, 5};
const uint8_t NPINS   = sizeof(PINS) / sizeof(PINS[0]);

struct Ev { uint32_t us; uint8_t pin; uint8_t state; };
const uint16_t QN = 2048;
volatile Ev       q[QN];
volatile uint16_t qHead = 0, qTail = 0;
volatile uint32_t dropped = 0;

static inline void push(uint8_t pin, uint8_t state) {
  uint32_t us = micros();
  uint16_t next = (uint16_t)((qHead + 1) % QN);
  if (next == qTail) { dropped++; return; }
  q[qHead].us = us; q[qHead].pin = pin; q[qHead].state = state;
  qHead = next;
}

// One ISR per pin so the pin number is known without a lookup.
void isr0() { push(PINS[0], digitalReadFast(PINS[0])); }
void isr1() { push(PINS[1], digitalReadFast(PINS[1])); }
void isr2() { push(PINS[2], digitalReadFast(PINS[2])); }
void isr3() { push(PINS[3], digitalReadFast(PINS[3])); }
void (*ISRS[])() = { isr0, isr1, isr2, isr3 };

void setup() {
  Serial.begin(115200);
  uint32_t t0 = millis();
  while (!Serial && millis() - t0 < 3000) {}
  for (uint8_t i = 0; i < NPINS; i++) {
    pinMode(PINS[i], INPUT);            // the external 20 k to GND acts as the pull-down
    attachInterrupt(digitalPinToInterrupt(PINS[i]), ISRS[i], CHANGE);
  }
  Serial.print("READY,YeLink_EdgeLogger,pins=");
  for (uint8_t i = 0; i < NPINS; i++) { if (i) Serial.print(','); Serial.print(PINS[i]); }
  Serial.println();
}

void loop() {
  // host requests
  while (Serial.available() > 0) {
    int c = Serial.read();
    if (c == 'T') {
      uint32_t us = micros();
      Serial.print("S,"); Serial.println(us);
      Serial.send_now();                // flush immediately so the sync round trip is short
    } else if (c == 'Q') {
      for (uint8_t i = 0; i < NPINS; i++) {
        Serial.print("P,"); Serial.print(micros()); Serial.print(',');
        Serial.print(PINS[i]); Serial.print(','); Serial.println(digitalReadFast(PINS[i]));
      }
      Serial.send_now();
    }
  }

  // drain the edge queue
  while (qTail != qHead) {
    Ev e;
    noInterrupts();
    e.us = q[qTail].us; e.pin = q[qTail].pin; e.state = q[qTail].state;
    qTail = (uint16_t)((qTail + 1) % QN);
    interrupts();
    Serial.print("E,"); Serial.print(e.us); Serial.print(',');
    Serial.print(e.pin); Serial.print(','); Serial.println(e.state);
  }

  static uint32_t lastDropReport = 0;
  if (dropped != lastDropReport) {
    lastDropReport = dropped;
    Serial.print("D,"); Serial.println(dropped);
  }
}