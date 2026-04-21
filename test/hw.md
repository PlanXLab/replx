# replx 하드웨어 테스트 시나리오

## 목차

- [공통 준비사항](#공통-준비사항)
- [GPIO](#gpio)
  - [write → scope 기본 검증](#gpio-1-write--scope-기본-검증)
  - [seq 펄스 → scope + delta 검증](#gpio-2-seq-펄스--scope--delta-검증)
  - [read pulse 측정 — 정밀 검증](#gpio-3-read-pulse-측정--정밀-검증)
  - [read wait — 크로스보드 트리거](#gpio-4-read-wait--크로스보드-트리거)
  - [실전 하드웨어 테스트 — LED / 스위치 / 초음파](#gpio-5-실전-하드웨어-테스트)
- [PWM](#pwm)
  - [write + monitor (2보드)](#pwm-1-write--monitor-2보드)
  - [seq 스위프 + monitor](#pwm-2-seq-스위프--monitor)
  - [서보모터 제어 (선택)](#pwm-3-서보모터-제어-선택)
- [ADC](#adc)
  - [GPIO 레벨 측정 (2보드)](#adc-1-gpio-레벨-측정-2보드)
  - [PWM → RC 필터 → ADC (2보드)](#adc-2-pwm--rc-필터--adc-2보드)
  - [scope — 인터랙티브 파형 스코프](#adc-3-scope--인터랙티브-파형-스코프)
  - [실전 하드웨어 — 가변저항 / 온도 센서 (선택)](#adc-4-실전-하드웨어-선택)
- [UART](#uart)
  - [루프백 (단일 보드)](#uart-1-루프백-단일-보드)
  - [양방향 2보드](#uart-2-양방향-2보드)
  - [monitor 모드](#uart-3-monitor-모드)
  - [hex 프레임 전송](#uart-4-hex-프레임-전송)
- [SPI](#spi)
  - [루프백 (단일 보드)](#spi-1-루프백-단일-보드)
  - [마스터 → 슬레이브 단방향](#spi-2-마스터--슬레이브-단방향)
  - [양방향 xfer (마스터 ↔ 슬레이브)](#spi-3-양방향-xfer)
  - [대용량 버스트 전송](#spi-4-대용량-버스트-전송)
  - [50 MHz 신뢰성 반복 검증](#spi-5-50-mhz-신뢰성-반복-검증)
- [I2C](#i2c)
  - [컨트롤러 ↔ 타깃 기본 통신 (2보드)](#i2c-1-컨트롤러--타깃-기본-통신-2보드)
  - [Write-Readback 무결성 검증](#i2c-2-write-readback-무결성-검증)
  - [타깃 메모리 프리로드 + 레지스터 덤프](#i2c-3-타깃-메모리-프리로드--레지스터-덤프)
  - [고속 400 kHz 반복 읽기](#i2c-4-고속-400-khz-반복-읽기)
  - [실전 하드웨어 — 센서 레지스터 읽기 (선택)](#i2c-5-실전-하드웨어--센서-레지스터-읽기-선택)
---

## 공통 준비사항

```
장비  : 마이크로파이썬 보드 2대 (Board A, Board B)
케이블: 점퍼 와이어 (GND 공통 결선 필수)
소프트: replx 설치 확인
```

> **GND 공통 결선은 모든 시나리오의 필수 조건이다.**  
> 전원이 서로 다른 보드끼리 GND를 연결하지 않으면 신호 기준 전위가 달라 통신 실패 및 소자 손상이 발생한다.

COM 포트 번호는 실제 환경에 맞게 대체한다.  
예: `COM1` → Board A,  `COM2` → Board B

---

## GPIO

> `gpio monitor` 는 디지털 스코프로, 14행 파형 + 8행 엣지 이벤트 로그를 실시간 표시한다.  
> 모든 GPIO 시나리오에서 **관찰·검증 도구**로 사용된다.

```
Board A (COM1)       Board B (COM2)
──────────────       ──────────────
GP1 (OUT) ─────────→ GP2 (IN)
GND       ══════════ GND
```

> GND 공통 결선 필수. 모든 시나리오에서 유지한다.

---

### GPIO-1. write → scope 기본 검증

`gpio write` 의 레벨 출력이 스코프 파형과 엣지 로그에 정확히 반영되는지 확인한다.

**Step 1 — Board B: 스코프 시작**
```sh
# Terminal 2
replx COM2 gpio monitor GP2
```
> 파형 전체가 초기 레벨로 채워지며, 엣지 로그는 `---` 상태다.  
> `last change: --` — 아직 엣지가 감지되지 않음을 나타낸다.

**Step 2 — Board A: High 출력**
```sh
# Terminal 1
replx COM1 gpio write GP1 1
```
스코프에서 확인:
- 파형에 상승 엣지 후 High 구간이 그려진다.
- 엣지 로그 1행: `XXXXXXXXX ms   ↑  rising        ` (첫 엣지 — delta 없음)
- `last change: ↑  at ... ms  held ... ms`

**Step 3 — Board A: Low 출력**
```sh
# Terminal 1
replx COM1 gpio write GP1 0
```
스코프에서 확인:
- 파형에 하강 엣지 후 Low 구간이 그려진다.
- 엣지 로그 2행: `XXXXXXXXX ms   ↓  falling    ⧗  NNN ms  H` (H 구간 유지 시간)

**Step 4 — Board A: 반복 토글**
```sh
# Terminal 1
replx COM1 gpio write GP1 1;replx COM1 gpio write GP1 0
```

**확인**
- 파형에 Step 2~4의 레벨 변화가 순서대로 그려진다.
- 엣지 로그 홀수 행(앵커): delta 없음
- 엣지 로그 짝수 행(클로징): `⧗ NNN ms H` 또는 `⧗ NNN ms L` 표시
- 9번째 이벤트부터 가장 오래된 줄이 순환 덮어써진다.

---

### GPIO-2. seq 펄스 → scope + delta 검증

`gpio seq` 로 펄스를 생성하고, 스코프 파형과 엣지 로그 delta 값으로 펄스폭을 검증한다.

> **IRQ 엣지 캡처** — 스코프는 `pin.irq()` 핸들러로 엣지를 캡처하므로 엣지가 누락되지 않는다.  
> IRQ 타임스탬프 정밀도는 **~10–50 µs** (MicroPython Python 레벨 디스패치 오버헤드)이다.  
> `--interval` ms 는 **렌더링 갱신 주기이자 스코프 시간 축 단위**이다. 1 frame = 1 tick = `interval` ms.  
> 파형 레벨은 IRQ 엣지 기록에서 추적하므로 `pin.value()` 레이스 오차가 없다.  
> 기본 interval = **10 ms**, canvas 78열 → 한 스윕 ≈ **780 ms** (3×230 ms 펄스 시퀀스 전체 표시 가능).  
> **`interval`보다 짧은 펄스**는 엣지 로그에 기록되지만 파형에 표시되지 않을 수 있다.  
> **`interval × 78`보다 긴 신호 시퀀스**는 캔버스를 넘어 오래된 펄스가 덮어씌워질 수 있다.

**Step 1 — Board B: 스코프 시작 (기본 interval)**
```sh
# Terminal 2
replx COM2 gpio monitor GP2
```

**Step 2 — Board A: 200 ms High 펄스 반복 생성**
```sh
# Terminal 1
replx COM1 gpio seq GP1 0 m50 1 m200 0
replx COM1 gpio seq GP1 0 m50 1 m200 0 m50 1 m200 0 m50 1 m200 0 
```
> `m50` = 50 ms low gap, `m200` = 200 ms High 구간.  
> 200 ms 펄스는 canvas 1.56 s 스윕에서 ≈10열 너비로 명확히 그려진다.

**확인**
- 파형에 High 펄스 구간이 반복적으로 나타난다.
- 엣지 로그 짝수 행(falling): `⧗  ~200 ms  H`

**Step 3 — 펄스폭 변경 비교**
```sh
# Terminal 1
replx COM1 gpio seq GP1 0 m50 1 m100 0
replx COM1 gpio seq GP1 0 m50 1 m500 0
replx COM1 gpio seq GP1 0 m50 1 m1000 0
```

**확인**: 파형에서 High 구간 너비가 커지고, 엣지 로그 delta 값이 순서대로 ≈100 ms → ≈500 ms → ≈1000 ms 로 변한다.

---

### GPIO-3. read pulse 측정 — 정밀 검증

`gpio read` 의 `pulse_h` / `pulse_l` 액션으로 µs 단위 정밀 측정을 검증한다.  
Board B 가 `pulse_h`로 펄스 폭을 측정하고, Board A 가 정해진 폭의 펄스를 생성한다.

> **오버헤드 주의** — `gpio seq` 는 보드에서 토큰을 순차 실행한다.  
> 마지막 딜레이(u500) 후 핀 쓰기(0) 까지 MicroPython 실행 오버헤드 **~1–10 µs** 가 더해진다.  
> `pulse_h` / `pulse_l` 측정은 `machine.time_pulse_us()` C 구현을 사용하므로  
> 수신 측 수신 오차는 **~1–2 µs** 수준이다. 지배적 오차는 송신 측 `wp.value()` 호출 오버헤드뿐이다.

> **전제 조건** — `pulse_h` 는 GP2 가 **Low** 상태일 때 Low→High 전환을 기다린다.  
> GP2 가 이미 High 이면 전환 대기를 건너뛰고 현재 High 구간을 그대로 측정하므로 엉뚱한 값이 나온다.  
> Terminal 2 를 시작하기 **전에** Terminal 1 에서 GP1=0 을 먼저 설정해야 한다.

**Step 1 — Board A: GP1 Low 설정 (전제 조건)**
```sh
# Terminal 1
replx COM1 gpio write GP1 0
```

**Step 2 — Board B: High 펄스 측정 대기**
```sh
# Terminal 2
replx COM2 gpio read GP2 pulse_h
```

**Step 3 — Board A: 500 µs High 펄스 생성**
```sh
# Terminal 1
replx COM1 gpio seq GP1 0 u10 1 u500 0
```

**확인**: Terminal 2 출력: `Pulse: ~5xx us` (송신 측 실행 오버헤드 ~1–30 µs)

**연속 측정 (--repeat)**
```sh
# Terminal 2 — 5회 반복 측정
replx COM2 gpio read GP2 pulse_h --repeat 5

# Terminal 2 — Ctrl+C 누를 때까지 무한 반복
replx COM2 gpio read GP2 pulse_h --repeat 0
```
> 반복 시 각 패널 제목에 `#1`, `#2`, … 순번이 표시된다.  
> Board A 에서 동일하게 `gpio seq` 를 반복 실행하여 각 측정에 대응한다.

---

**pulse_l 측정**

> **전제 조건** — `pulse_l` 은 GP2 가 **High** 상태일 때 High→Low 전환을 기다린다.  
> GP2 가 이미 Low 이면 전환 대기를 건너뛰고 현재 Low 구간을 그대로 측정하므로 엉뚱한 값이 나온다.  
> Terminal 2 를 시작하기 **전에** Terminal 1 에서 GP1=1 을 먼저 설정해야 한다.

**Step 1 — Board A: GP1 High 설정 (전제 조건)**
```sh
# Terminal 1
replx COM1 gpio write GP1 1
```

**Step 2 — Board B: Low 펄스 측정 대기**
```sh
# Terminal 2
replx COM2 gpio read GP2 pulse_l
```

**Step 3 — Board A: 200 µs Low 펄스 생성**
```sh
# Terminal 1
replx COM1 gpio seq GP1 1 u10 0 u200 1
```
Terminal 2 출력: `Pulse: ~2xx us` (송신 측 실행 오버헤드 ~1–10 µs)

---

**응용 — 초음파 센서 거리 환산**

> `pulse_h` 와 동일하게 GP2 가 **Low** 상태에서 시작해야 한다.

```sh
# Terminal 1 — GP1 Low 설정 (전제 조건)
replx COM1 gpio write GP1 0

# Terminal 2 — 측정 대기
replx COM2 gpio read GP2 pulse_h --expr "pulse_us/58"

# Terminal 1 — 2320 µs High 펄스 생성
replx COM1 gpio seq GP1 0 u10 1 u2320 0
```
결과: `≈40.0` (40 cm에 해당)

---

### GPIO-4. read wait — 크로스보드 트리거

Board B 가 Board A 의 신호를 기다렸다가 조건 충족 즉시 반환하는 핸드셰이크 패턴을 검증한다.

```sh
# Terminal 1 — GP1 Low 설정 (전제 조건)
replx COM1 gpio write GP1 0
```

**Step 1 — Board B: wait_h 대기**
```sh
# Terminal 2
replx COM2 gpio read GP2 wait_h
```
> GP2 가 High 가 될 때까지 블로킹 대기한다.

**Step 2 — Board A: 트리거 발생**
```sh
# Terminal 1
replx COM1 gpio write GP1 1
```

**확인**: Terminal 2 가 즉시 반환한다.

---

**wait_l 검증**

```sh
# Terminal 1 — GP1 HIGH 설정 (전제 조건)
replx COM1 gpio write GP1 1
```

```sh
# Terminal 2
replx COM2 gpio read GP2 wait_l

# Terminal 1 (GP1 이 현재 High 상태)
replx COM1 gpio write GP1 0
```
Terminal 2 즉시 반환.

---

**취소 확인**

```sh
# Terminal 1 — GP1 Low 설정 (전제 조건)
replx COM1 gpio write GP1 0
```

```sh
# Terminal 2 — 신호 없이 대기 후 Ctrl+C 로 취소
replx COM2 gpio read GP2 wait_h
```
Board A 에서 신호를 보내지 않으면 무한 대기하므로, Ctrl+C 를 눌러 취소한다.

---


### GPIO-5. 실전 하드웨어 테스트

단일 보드(Board A, COM1)에 실제 소자를 연결해 검증한다.

> **외부 전원 사용 시 필수**: LED 모듈, 스위치 모듈, 초음파 센서 등이 별도 전원 보드(외부 5V 어댑터, USB 허브, 모듈 전용 전원 등)에서 전원을 공급받는 경우,  
> **해당 전원 보드의 GND 를 마이크로파이썬 보드의 GND 와 반드시 공유해야 한다.**  
> GND 가 분리되면 신호 기준 전위가 달라져 GPIO 읽기/쓰기가 오동작하고, 경우에 따라 과전압으로 핀이 손상된다.

---

#### GPIO-5a. LED 점멸

```
Board A (COM1)
──────────────
GP1 (OUT) ──[220Ω]──→ LED Anode
                       LED Cathode → GND (보드 GND)
```

> 저항 없이 직결하면 과전류로 LED 및 GPIO 핀이 손상된다. 3.3V 기준 **220 Ω ~ 470 Ω** 권장.  
> LED 를 외부 드라이버 보드로 구동하는 경우, 드라이버 보드의 GND 를 Board A GND 와 연결한다.

**켜기 · 끄기**

```sh
replx COM1 gpio write GP1 1   # LED ON
replx COM1 gpio write GP1 0   # LED OFF
```

**500 ms 점멸 시퀀스**

```sh
replx COM1 gpio seq GP1 1 m500 0
```

**반복 점멸 (300 ms ON / 300 ms OFF, Ctrl+C 로 중단)**

```
Board A (COM1)       Board B (COM2)
──────────────       ──────────────
GP1 (OUT) ──[220Ω]──→ LED Anode
           |            LED Cathode → GND (보드 GND)
GP1 (OUT)  ────────→ GP2 (IN)
```

> 보드 A/B와 LED는 반드시 GND를 공유해야 한다.
> 보드 A의 GP1 출력은 별도 조인트 모듈을 통해 LED와 보드 B에 함께 연결되어야 한다.

```sh
# Terminal 2 — 스코프로 파형 확인 (선택)
replx COM2 gpio monitor GP2
```

```sh
# Terminal 1
replx COM1 gpio seq GP1 1 m300 0 m300 1 m300 0 m300 1 m300 0 m300
```

**확인**: LED 가 켜고 꺼지는 사이클이 반복되고, 스코프에 300 ms 폭의 High/Low 구간이 번갈아 그려진다.

---

#### GPIO-5b. 택트 스위치 읽기

```
Board A (COM1)
──────────────
GP1 (IN, PULL_UP) ──┬── 스위치 ── GND (보드 GND)
                    │
                (내부 풀업)
```

> 내부 풀업(`Pin.PULL_UP`)을 사용하므로 외부 저항이 필요 없다. 평상시 GP1 = High, 스위치 누름 = Low.  
> 스위치 모듈이 외부 VCC 기반 풀업을 사용하는 경우, **해당 모듈의 GND 를 Board A GND 와 공유해야 한다.**  
> 그렇지 않으면 논리 레벨 기준이 어긋나 `wait_h` / `wait_l` 이 오동작한다.

**스코프로 실시간 감지**

```sh
replx COM1 gpio monitor GP1
```

스위치를 누르면 파형에 Low 구간이 그려지고, 엣지 로그에 `↓ falling` / `↑ rising` 이 기록된다.

**`wait_l` — 누름 이벤트 감지**

```sh
replx COM1 gpio read GP1 wait_l
```

GP1 이 Low(스위치 눌림)가 될 때까지 블로킹 대기 후 즉시 반환한다. 스위치를 누르면 결과가 출력된다.

**`wait_h` — 떼기 이벤트 감지**

```sh
# 전제 조건: 스위치를 누른 채로 아래 명령 실행
replx COM1 gpio read GP1 wait_h
```

스위치를 놓으면(GP1 → High) 즉시 반환한다.

**취소 확인**

```sh
replx COM1 gpio read GP1 wait_l
```

스위치를 누르지 않고 Ctrl+C 를 눌러 취소한다. 명령이 즉시 중단된다.

**`pulse_h --repeat` 와 스위치: round-trip 누락 현상**

> `pulse_h --repeat N` 은 매 측정마다 보드에서 스크립트를 새로 실행하고 결과를 수신한다.  
> 스크립트가 결과를 출력한 시점부터 다음 스크립트가 시작될 때까지 **round-trip 지연(약 400–600 ms)** 이 발생한다.  
> 이 구간에 발생한 누름·떼기 이벤트는 새 스크립트가 시작되지 않은 상태이므로 **검출되지 않는다.**
>
> 예) 1 Hz(약 1회/초) 로 누를 때 round-trip 이 500 ms 면, 매 500 ms 마다 1회만 검출되므로 2–3회 연속 누름 중 1회만 출력된다.
>
> `pulse_h --repeat` 는 외부에서 제어 타이밍을 맞출 수 있는 신호(HC-SR04 ECHO 등)에 적합하다.  
> **스위치처럼 사람이 임의로 누르는 이벤트를 빠짐없이 검출하려면 `gpio monitor` 를 사용한다.**  
> `gpio monitor` 는 보드가 계속 실행 중인 IRQ 기반 스코프이므로 round-trip 없이 모든 엣지를 캡처한다.

---

#### GPIO-5c. HC-SR04 초음파 센서 거리 측정

HC-SR04 는 TRIG 에 10 µs 이상의 High 펄스를 주면, ECHO 핀에 거리에 비례하는 High 펄스를 돌려준다.  
거리(cm) = pulse_us / 58

```
HC-SR04           Board A (COM1)
───────           ──────────────
VCC  ←── 5V  (보드 VBUS 핀 또는 별도 전원)
GND  ════════════ GND  ← 별도 전원 사용 시 공통 결선 필수
TRIG ←──────────── GP1 (OUT)
ECHO ──[1kΩ]──┬── GP2 (IN)
              └──[2kΩ]── GND
```

> **전압 주의**: HC-SR04 의 ECHO 핀은 5V 출력이다. 3.3V MCU(RP2040 등)에 직결하면 GPIO 핀이 손상될 수 있다.  
> 반드시 **1 kΩ + 2 kΩ 전압 분배** 또는 레벨 시프터를 통해 3.3V 이하로 낮춰 연결한다.  
>
> **GND 공유**: 보드 VBUS(5V) 핀으로 센서를 구동하면 이미 GND 가 동일하다.  
> 외부 USB 어댑터 등 **별도 전원**을 사용하는 경우, **그 전원의 GND 를 반드시 Board A GND 에 연결한다.**  
> GND 가 분리되면 ECHO 신호 기준이 달라져 `pulse_h` 측정이 전혀 반응하지 않거나 오동작한다.

**단일 측정**

`gpio seq` 는 출력 시퀀스 실행 직후 다른 핀의 read_action 을 이어서 수행할 수 있다.  
TRIG 출력(GP1)과 ECHO 측정(GP2)을 한 명령 안에서 순차 처리한다.

```sh
replx COM1 gpio seq GP1 0 u5 1 u10 0 GP2 pulse_h --expr "pulse_us/58"
```

**확인**: `≈ N.N` 이 출력된다.

**연속 측정**

```sh
# 500ms 간격으로 HC-SR04 무한 측정
replx COM1 gpio seq GP1 0 u5 1 u10 0 GP2 pulse_h --expr "pulse_us/58" --repeat 0 --interval 500
```

---


## PWM

> RP2350/RP2040: `pwm monitor` 는 PIO 125 MHz 원자적 측정 방식을 사용한다. 기타 보드: `time_pulse_us()` 폴백.  
> 측정 주파수 범위: **20 Hz – 20000 Hz**.

---

### PWM-1. write (2보드)

Board A 가 PWM 신호를 출력하고, Board B 가 실시간으로 주파수·듀티·펄스 폭을 측정·기록한다.

```
Board A (COM1)        Board B (COM2)
──────────────        ──────────────
GP1  (PWM OUT) ──────→ GP2 (IN)
GND            ════════ GND
```

**Step 1 — Board B: monitor 시작**
```sh
# Terminal 2
replx COM2 pwm monitor GP2
```
화면이 클리어되고 상단 2줄(헤더 + 구분선)이 고정된다. 신호가 들어오면 3행부터 변경 시점마다 1줄씩 로그가 스크롤되며, 헤더는 항상 화면 상단에 유지된다.

**Step 2 — Board A: PWM 출력 시작**
```sh
# Terminal 1
replx COM1 pwm write GP1 --freq 1000 --duty-percent 50
```

**확인**: Board B 화면에 다음과 같은 로그가 출력됨
```
  00:00:xx   1000 Hz   1000 us     500.0 us     500.0 us  32768/65535  50.00% 
```

**Step 3 — 듀티 변경**
```sh
# Terminal 1
replx COM1 pwm write GP1 --freq 1000 --duty-percent 25
replx COM1 pwm write GP1 --freq 1000 --duty-percent 75
```

각 변경마다 새 로그 줄이 추가된다. 전환 윈도우(혼합 측정값)는 자동으로 skip 되고 안정된 값만 기록된다.

**Step 4 — 주파수 변경**
```sh
replx COM1 pwm write GP1 --freq 100  --duty-percent 50
replx COM1 pwm write GP1 --freq 10000 --duty-percent 50
```

Freq, Period, High, Low 수치가 함께 바뀌고, Duty 는 50.00% 유지되는 것을 확인한다.

**Step 5 — DC HIGH / DC LOW 확인**
```sh
replx COM1 pwm write GP1 --freq 1000 --duty-percent 100
replx COM1 pwm write GP1 --freq 1000 --duty-percent 0
```

```
  00:00:xx  DC HIGH   Duty: 100.00%
  00:00:xx  DC LOW    Duty:   0.00%
```

**Step 6 — 정리**
```sh
replx COM1 pwm stop GP1
```
Board B 에 `DC LOW` 이 기록된다. Ctrl+C 로 monitor 종료.

---

### PWM-2. seq 스위프 (2보드)

`seq` 를 이용해 듀티를 단계적으로 변화 시키면서 monitor 에서 각 단계를 확인한다.

**Step 1 — Board B: monitor 시작**
```sh
# Terminal 2
replx COM2 pwm monitor GP2
```

**Step 2 — Board A: 0 → 100% 선형 스위프 (300 ms 간격)**
```sh
# Terminal 1
replx COM1 pwm seq GP1 --freq 1000 --duty percent 0 m300 10 m300 20 m300 30 m300 40 m300 50 m300 60 m300 70 m300 80 m300 90 m300 100
```

**확인**: Board B 에 각 단계별 로그 줄이 순차적으로 출력된다.

**Step 3 — 반복 사이클 (100 → 0% 역방향)**
```sh
replx COM1 pwm seq GP1 --freq 1000 --duty percent 100 m300 90 m300 80 m300 70 m300 60 m300 50 m300 40 m300 30 m300 20 m300 10 m300 0
```

**Step 4 — pulse_us 모드 (서보 펄스 폭 스위프)**
```sh
replx COM1 pwm seq GP1 --freq 50 --duty pulse_us 500 m700 1250 m500 1500 m500 1750 m500 2500
```

monitor 에서 각 구간의 duty% 가 `2.50% → 6.25% → 7.50% → 8.75% → 12.50%` 순으로 변하는 것을 확인한다  
(50 Hz 주기 = 20 ms 기준).

**Step 5 — --repeat 반복 테스트**
```sh
# 3회 반복 (유한)
replx COM1 pwm seq GP1 --freq 50 --duty pulse_us 500 m700 1250 m500 1500 m500 1750 m500 2500 --repeat 3

# 무한 반복 (Ctrl+C로 중단)
replx COM1 pwm seq GP1 --freq 50 --duty pulse_us 500 m700 1250 m500 1500 m500 1750 m500 2500 --repeat 0
```

**확인**:
- `--repeat 3`: 화면이 클리어되고 고정 헤더(핀/주파수/패턴/repeat 정보)와 구분선이 상단 2줄에 표시된다.
  데이터 영역에 `HH:MM:SS   #1` ~ `#3` 이 출력된 뒤 `Done: 3 cycles, 15 writes` 완료 줄이 출력된다.
  헤더는 스크롤되지 않고 고정된 상태를 유지한다.
- `--repeat 0`: 동일 고정 헤더가 표시되고 데이터 영역에 `HH:MM:SS   #1`, `#2`, ... 가 계속 스크롤된다.
  헤더는 화면 상단에 고정. Ctrl+C로 정상 중단.

---

### PWM-3. 서보모터 제어 

서보모터를 Board A 에 직접 연결하고, Board B 의 monitor 로 펄스 폭을 동시에 확인한다.

```
Board A (COM1)           Board B (COM2)
──────────────           ──────────────
GP1 (PWM) ──────┬──────→ GP2 (IN)
                └──────→ 서보모터 신호선
GND        ════════════  GND (+ 서보 GND)
```

> 서보모터는 일반적으로 5 V 전원을 따로 사용한다. 신호선만 보드 GPIO 와 연결.

**Step 1 — Board B: monitor 시작**
```sh
replx COM2 pwm monitor GP2
```

**Step 2 — 서보 중립 (1500 µs)**
```sh
replx COM1 pwm write GP1 --freq 50 --pulse-us 1500
```
monitor:
```
  00:00:xx     50 Hz   20000 us    1500.0 us   18500.0 us   4915/65535   7.50% 
```

**Step 3 — 좌측 끝 (1000 µs) → 우측 끝 (2000 µs)**
```sh
replx COM1 pwm write GP1 --freq 50 --pulse-us 1000
replx COM1 pwm write GP1 --freq 50 --pulse-us 2000
```

**Step 4 - 300Hz로 제어(서보 모터 내부 프로세서가 최대 250Hz~333Hz을 지원하는 제품에서 테스트할 것!)**
```sh
replx COM1 pwm write GP1 --freq 300 --pulse-us 500
replx COM1 pwm write GP1 --freq 300 --pulse-us 2500
replx COM1 pwm seq GP1 --freq 300 --duty pulse_us 500 m800 1250 m500 1500 m500 1750 m500 2500 m700 --repeat 0
```

**Step 5 — 정리**
```sh
replx COM1 pwm stop GP1
```

---


## ADC

> ADC 채널은 RP2350 기준 GP26 (ADC0), GP27 (ADC1), GP28 (ADC2) 핀을 사용한다.  
> `read_u16()` 반환값은 0–65535 (16비트 스케일); 전압 환산: V = raw × Vref / 65535.  
> `adc scope` 는 보드에 `termviz`, `ufilter` 모듈이 설치되어 있어야 한다.

---

### ADC-1. GPIO 레벨 측정 (2보드)

Board A 의 GPIO 출력(0 V / 3.3 V)을 Board B 의 ADC 핀으로 측정하여 `adc read` 의 기본 동작을 검증한다.

```
Board A (COM1)       Board B (COM2)
──────────────       ──────────────
GP1  (OUT) ─────────→ GP26 (ADC0, IN)
GND        ══════════ GND
```

**Step 1 — Board A: GP1 Low (0 V) 설정**
```sh
# Terminal 1
replx COM1 gpio write GP1 0
```

**Step 2 — Board B: ADC 읽기 (0 V 기대)**
```sh
# Terminal 2
replx COM2 adc read GP26
```
출력 예: `GP26  raw=[  xxx]  V=[0.0xx]` (0 V 근처, GND 잔류 노이즈로 ±0.1 V 오차 허용)

**Step 3 — Board A: GP1 High (3.3 V) 설정**
```sh
# Terminal 1
replx COM1 gpio write GP1 1
```

**Step 4 — Board B: ADC 읽기 (3.3 V 기대)**
```sh
# Terminal 2
replx COM2 adc read GP26
```
출력 예: `GP26  raw=[65nnn]  V=[3.2xx]` (3.3 V 근처)

**Step 5 — 반복 읽기 비교**
```sh
# Terminal 1 — Low 출력
replx COM1 gpio write GP1 0
# Terminal 2 — 5회 읽기 (500 ms 간격)
replx COM2 adc read GP26 --repeat 5 --interval 500

# Terminal 1 — High 출력
replx COM1 gpio write GP1 1
# Terminal 2 — 무한 반복 (Ctrl+C로 중단)
replx COM2 adc read GP26 --repeat 0 --interval 500
```

**확인**
- GP1=0 → raw ≈ 0, V ≈ 0.0
- GP1=1 → raw ≈ 65535, V ≈ 3.3

---

### ADC-2. scope — 인터랙티브 파형 스코프

`adc scope` 는 보드에서 실시간 ADC 파형 뷰어를 실행한다. 파형, 필터, 샘플 속도를 키보드로 인터랙티브하게 제어한다.

> **사전 조건**: 보드에 `termviz`, `ufilter` 두 모듈이 설치되어 있어야 한다.
> ADC-2 연결을 유지하거나, GP26 에 가변저항을 연결하면 파형 변화를 직접 확인할 수 있다.

**Step 1 — 단일 채널 스코프**
```sh
# Terminal 2
replx COM2 adc scope GP26
```
스코프 화면이 열리고 GP26 의 아날로그 파형이 실시간으로 그려진다. 하단 상태 바에 현재 전압(Now), 최솟값(Min), 최댓값(Max), 피크투피크(Vpp) 가 표시된다.

**Step 2 — 샘플 속도 변경**
`LEFT` / `RIGHT` 키로 렌더 갱신 속도(SAMP) 를 순환한다: `max · 1ms · 2ms · 5ms · 10ms · 20ms · 50ms · 100ms`.  
실제 샘플링은 항상 ~1 ms 최대 속도로 유지되며, SAMP 는 화면 갱신 주기만 조절한다.

**Step 3 — 필터 전환 (ENTER)**
`ENTER` 키로 필터 편집 메뉴를 연다. `UP` / `DOWN` 으로 필터 그룹을 선택하고 `LEFT` / `RIGHT` 로 그룹 내 필터를 전환한다.  
파라미터 편집은 그룹 선택 후 `ENTER` 를 한 번 더 눌러 진입하고, `ESC` 로 메뉴를 닫는다.

| 그룹 | 포함 필터 |
|------|-----------|
| RAW | 원시 값 (필터 없음) |
| FILT1 | EMA, MV, MED, RMS |
| FILT2 | Kalman, Kalman1D, Adaptive, TauLowPass |
| FILT3 | LowPass, HighPass, Butterworth-LP, Butterworth-HP |

**Step 4 — 다채널 스코프 (기본 GP26 · GP27 · GP28)**
```sh
# Terminal 2 — 핀 생략 시 기본 GP26 GP27 GP28
replx COM2 adc scope
```
채널 전환: `1` / `2` / `3` 키. 각 채널의 파형이 서로 다른 색으로 겹쳐 표시된다.  
다채널 모드에서 Y 축은 0.0–1.0 (정규화)으로 고정되며 통계 바도 채널별로 분리 출력된다.  
연결되지 않은 핀(부유 상태)은 노이즈 파형이 나타나는 것이 정상이다.

**Step 5 — 종료**
`Ctrl+C` 로 스코프를 종료한다.

---

### ADC-4. 실전 하드웨어 (선택)

단일 보드(Board B, COM2)에 실제 아날로그 소자를 연결해 검증한다.


#### ADC-4a. 가변저항 (전압 분배)

```
Board B (COM2)
──────────────
3.3V ──── 가변저항 한쪽 끝
GND  ──── 가변저항 다른 쪽 끝
GP26 (ADC0) ──── 가변저항 중앙 와이퍼
```

> 보드 3.3V 핀에서 전원을 공급한다. 가변저항 저항값은 1 kΩ – 100 kΩ 범위에서 무관하다.

**무한 반복 (Ctrl+C로 중단)**
```sh
replx COM2 adc read GP26 --repeat 0 --interval 500
```

**스코프로 실시간 확인**

```sh
replx COM2 adc scope GP26
```
가변저항을 돌리면 파형이 실시간으로 상승·하강한다. 빠르게 돌릴수록 파형의 기울기가 급해진다.

---

#### ADC-4b. LM35 온도 센서

LM35 는 10 mV/°C 특성의 선형 온도 센서다. 25 °C 에서 출력 전압 = 250 mV, 전압 측정 후 × 100 하면 섭씨 온도가 된다.

```
LM35              Board B (COM2)
────              ──────────────
VCC ←───────────── 3.3V (보드 3.3V 핀 또는 VBUS)
GND ═══════════════ GND
OUT ────────────→ GP26 (ADC0)
```

> 3.3V 공급 시 측정 상한 ≈ 330 °C (센서 사양 범위 내). 실내 온도 측정에는 충분하다.  
> 외부 전원(5V 등)을 사용하는 경우 반드시 GND 를 Board B GND 와 공유해야 한다.

**단일 측정**
```sh
replx COM2 adc read GP26
```
출력 예: `V=[0.252]` → 25.2 °C (V 값 × 100)

**반복 측정**
```sh
replx COM2 adc read GP26 --repeat 0 --interval 10000
```

**스코프로 온도 변화 추적**
```sh
replx COM2 adc scope GP26
```
센서 핀 주변에 따뜻한 바람을 쐬거나 손가락으로 가볍게 접촉하면 전압이 서서히 상승하는 것을 파형으로 확인할 수 있다.

---


## UART

### UART-1. 루프백 (단일 보드)

TX 에서 전달한 데이터가 RX 로 돌아오는지 검증한다. RP2350에서 UART용 GPIO 번호는 반드시 UART로 예약된 번호를 사용해야 한다.

```
Board A (COM1)
GP0 (TX) ─┐
           └─  점퍼 와이어
GP1 (RX) ─┘
```

```sh
replx COM1 uart open --tx GP0 --rx GP1 --baud 115200
replx COM1 uart xfer "Hello"
replx COM1 uart xfer "Hello" --rx-bytes 3 --timeout 500
replx COM1 uart xfer --hex 01 02 03 04 --rx-bytes 4
replx COM1 uart close
```

**확인**: `xfer` 응답에서 송신된 문자열 / 바이트가 그대로 수신되어야 한다.

---

### UART-2. 양방향 2보드

Board A 가 전송하고 Board B 가 수신하며, 이후 반대 방향도 확인한다.

```
Board A (COM1)        Board B (COM2)
──────────────        ──────────────
GP0 (TX) ───────────→ GP1 (RX)
GP1 (RX) ←─────────── GP0 (TX)
GND      ═════════════ GND
```

**Step 1 — 두 보드 동시 설정**
```sh
# Terminal 1
replx COM1 uart open --tx GP0 --rx GP1 --baud 115200

# Terminal 2
replx COM2 uart open --tx GP0 --rx GP1 --baud 115200
```

**Step 2 — Board B: 수신 대기**
```sh
# Terminal 2
replx COM2 uart read --timeout 5000
```

**Step 3 — Board A: 전송**
```sh
# Terminal 1
replx COM1 uart write "TiCLE Board A"
```

**확인**: Terminal 2 에 `TiCLE Board A` 가 수신 출력된다.

**Step 4 — 반대 방향 확인**
```sh
# Terminal 1
replx COM1 uart read --timeout 3000

# Terminal 2
replx COM2 uart write "TiCLE Board B"
```

**Step 5 - 종료**
```sh
replx COM1 uart close
replx COM2 uart close
```
---

### UART-3. monitor 모드

Board B 가 `monitor` 로 연속 수신 대기하는 동안 Board A 가 복수 패킷을 전송한다.

```
Board A (COM1) ──GP0→GP1──> Board B (COM2)
```

**Step 1 — Board B: 모니터 시작**
```sh
# Terminal 2
replx COM2 uart open --tx GP0 --rx GP1 --baud 115200
replx COM2 uart monitor --idle 200
```
> `--idle 200`: 200 ms 무신호 구간에 구분선을 삽입한다.

**Step 2 — Board A: 여러 패킷 전송**
```sh
# Terminal 1
replx COM1 uart open --tx GP0 --rx GP1 --baud 115200
replx COM1 uart write "Packet-1"
replx COM1 uart write --hex DE AD BE EF 00 01
replx COM1 uart write "End"
```

**Text + Chunk 복합 모드**
```sh
# Terminal 2
replx COM2 uart monitor --text --chunk
```
패킷마다 타임스탬프와 바이트 길이 헤더가 표시된다.

**Hex 덤프 폭 변경**
```sh
replx COM2 uart monitor --width 8
```

---

### UART-4. hex 프레임 전송

Modbus RTU 등 바이너리 프로토콜 모의 시나리오.

```
Board A (COM1) ──────────────→ Board B (COM2)
 TX(GP0)                         RX(GP1)
```

**Modbus RTU 모의 요청 (슬레이브 주소 01, Read Holding Registers)**
```sh
# Terminal 1 — 요청 프레임 전송
replx COM1 uart write --hex 01 03 00 00 00 0A C5 CD

# Terminal 2 — 응답 대기 (예: 24바이트 응답 수신)
replx COM2 uart read 8 --timeout 3000
```

**xfer (전송 + 수신 한 번에)**
```sh
replx COM2 uart read --timeout 5000
replx COM1 uart xfer --hex 01 03 00 00 00 0A C5 CD --rx-bytes 24 --timeout 5000
replx COM2 uart write --hex 01 03 14 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 A0 31
```

**종료**
```sh
replx COM1 uart close
replx COM2 uart close
```

---

## SPI

> **슬레이브(Slave) 모드는 RP2350 보드에서만 지원한다.**  
> `spi open --slave` 를 RP2350 이외의 보드에서 실행하면 오류로 종료된다.

---

### SPI-1. 루프백 (단일 보드)

MOSI 와 MISO 를 직결하여 마스터가 송신한 데이터를 즉시 수신하는지 검증한다.

```
Board A (COM1)
GP3 (MOSI) ─┐
             └─  점퍼 와이어
GP4 (MISO) ─┘
```

```sh
replx COM1 spi open --sck GP2 --mosi GP3 --miso GP4 --baud 10000000
replx COM1 spi xfer 01 02 03 04 05
replx COM1 spi xfer AA BB CC DD EE FF
replx COM1 spi close
```

**확인**: `xfer` 응답의 수신 바이트가 송신 바이트와 동일해야 한다.

---

### SPI-2. 마스터 → 슬레이브 단방향

Board A(마스터)가 데이터를 전송하고, Board B(슬레이브, RP2350)가 수신하는 기본 단방향 시나리오.
replx-libs의 슬레이브는 RP2350 PIO로 구현되었으며, 최대 50Mbps까지 테스트

```
Board A (COM1, Master)      Board B (COM2, Slave, RP2350)
──────────────────────      ─────────────────────────────
GP2  SCK  ─────────────→    GP2  SCK
GP3  MOSI ─────────────→    GP3  MOSI   (MOSI = SCK+1 필수)
GP5  CS   ─────────────→    GP5  CS
GND       ════════════════   GND
```

> **슬레이브 핀 제약**: MOSI 핀 번호 = SCK 핀 번호 + 1 (PIO 설계상 필수)  
> 예: SCK=GP2 이면 MOSI=GP3

**Step 1 — Board B: 슬레이브 열기**
```sh
# Terminal 2
replx COM2 spi open --slave --sck GP2 --mosi GP3 --cs GP5
```

**Step 2 — Board B: 프레임 수신 대기**
```sh
# Terminal 2
replx COM2 spi read --timeout 10000
```

**Step 3 — Board A: 마스터 열기 + 전송**
```sh
# Terminal 1
replx COM1 spi open --sck GP2 --mosi GP3 --miso GP4 --baud 50000000
replx COM1 spi write 01 02 03 04 05 06 07 08 --cs GP5
```

**확인**: Board B 에 `01 02 03 04 05 06 07 08` 이 수신 출력된다.

**정리**
```sh
# Terminal 1
replx COM1 spi close

# Terminal 2
replx COM2 spi close
```

---

### SPI-3. 양방향 xfer

마스터가 MISO 경로도 활용하여 슬레이브와 동시에 데이터를 주고받는 전이중(Full-Duplex) 시나리오.

```
Board A (COM1, Master)      Board B (COM2, Slave, RP2350)
──────────────────────      ─────────────────────────────
GP2  SCK  ─────────────→    GP2  SCK
GP3  MOSI ─────────────→    GP3  MOSI
GP4  MISO ←─────────────    GP4  MISO
GP5  CS   ─────────────→    GP5  CS
GND       ════════════════   GND
```

**Step 1 — Board B: 슬레이브 열기 (MISO 포함)**
```sh
# Terminal 2
replx COM2 spi open --slave --sck GP2 --mosi GP3 --miso GP4 --cs GP5
```

**Step 2 — Board B: MISO 데이터 선적재 + 수신 대기**
```sh
# Terminal 2
replx COM2 spi write A0 A1 A2 A3 A4 A5 A6 A7 A8 A9 AA AB AC AD AE AF B0 B1 B2 B3 B4 B5 B6 B7 B8 B9 BA BB BC BD BE BF
replx COM2 spi read --timeout 10000
```
> `write` 는 슬레이브의 MISO 버퍼에 데이터를 미리 로드한다.  
> 이후 마스터가 클록을 전송하는 순간 슬레이브가 MISO 로 해당 바이트를 출력하면서 동시에 MOSI 를 수신한다.

**Step 3 — Board A: 마스터 열기 + xfer**
```sh
# Terminal 1
replx COM1 spi open --sck GP2 --mosi GP3 --miso GP4 --baud 5000000
replx COM1 spi xfer 00 01 02 03 04 05 06 07 08 09 0A 0B 0C 0D 0E 0F 10 11 12 13 14 15 16 17 18 19 1A 1B 1C 1D 1E 1F --cs GP5
```

**확인**
- Board A `xfer` 수신: `A0 A1 A2 A3 A4 A5 A6 A7 A8 A9 AA AB AC AD AE AF B0 B1 B2 B3 B4 B5 B6 B7 B8 B9 BA BB BC BD BE BF` (슬레이브가 선적재한 값)
- Board B `read` 수신: `00 01 02 03 04 05 06 07 08 09 0A 0B 0C 0D 0E 0F 10 11 12 13 14 15 16 17 18 19 1A 1B 1C 1D 1E 1F` (마스터가 전송한 값)

**`xfer` 단일 명령으로 MISO 선적재 + 수신 동시 처리**
```sh
# Terminal 2 (Step 2 대체)
replx COM2 spi xfer AA BB CC DD --timeout 10000
```
> `xfer` 는 `write` (MISO 선적재) + `read` (MOSI 수신)를 하나의 명령으로 수행한다.

**확인**
- Board A `xfer` 수신: `AA BB CC DD FF FF FF FF FF FF FF FF FF FF FF FF FF FF FF FF FF FF FF FF FF FF FF FF FF FF FF FF` (슬레이브가 선적재한 값을 제외하면 모두 FF)
- Board B `read` 수신: `00 01 02 03` (마스터가 전송한 값)

**정리**
```sh
# Terminal 1
replx COM1 spi close

# Terminal 2
replx COM2 spi close
```

---

### SPI-4. 대용량 버스트 전송

4096 바이트 버스트를 전송하여 DMA 연속 수신이 올바른지 확인한다.

```
Board A (COM1, Master)      Board B (COM2, Slave, RP2350)
GP2 SCK / GP3 MOSI / GP5 CS ──────→ GP2 SCK / GP3 MOSI / GP5 CS
GND ════════════════════════════════ GND
```

**Step 1 — Board B: 슬레이브 열기 (버퍼 8192 바이트)**
```sh
# Terminal 2
replx COM2 spi open --slave --sck GP2 --mosi GP3 --cs GP5 --slave-buf 8192
replx COM2 spi read --timeout 15000
```

**Step 2 — Board A: 10 MHz, 4096 바이트 전송**
```sh
# Terminal 1
replx COM1 spi open --sck GP2 --mosi GP3 --miso GP4 --baud 50000000
replx COM1 spi write $(python -c "print(' '.join(f'{i%256:02X}' for i in range(4096)))") --cs GP5
```

**확인**: Board B 수신 데이터가 `00 01 02 … FF 00 01 …` 패턴으로 4096 바이트 일치해야 한다.

**정리**
```sh
replx COM1 spi close
replx COM2 spi close
```

---

## I2C

**전제 조건**: MicroPython v1.26 이상.  
I2C 타깃 모드(`--target`)는 `machine.I2CTarget` API 를 사용하며, MicroPython v1.26 이상을 지원하는 모든 보드에서 동작한다.  
GP12/GP13 (SDA/SCL) 핀 양 끝에 **4.7 kΩ 풀업**(또는 보드 내장 풀업) 이 있어야 한다.

---

### I2C-1: 컨트롤러 ↔ 타깃 기본 통신 (2보드)

Board A(컨트롤러)가 Board B(타깃)에 데이터를 쓰고 다시 읽는 기본 양방향 통신을 검증한다. Board B는 I2CTarget으로 동작하며 주소 `0x42`, 메모리 256바이트.

**연결**:
```
Board A GP12 SDA ────── Board B GP12 SDA
Board A GP13 SCL ────── Board B GP13 SCL
GND ════════════════════ GND
```
(풀업 저항 4.7 kΩ 한쪽에만 달아도 됨)

**Step 1 — Board B: 타깃 열기**
```sh
# Terminal 2
replx COM2 i2c open --sda GP12 --scl GP13 --target --addr 0x42 --mem-size 256
```
결과 패널: `Mode: TARGET  I2C Addr: 0x42 / mem 256 bytes`

**Step 2 — Board A: 버스 스캔 (Board B 타깃이 보여야 함)**
```sh
# Terminal 1
replx COM1 i2c scan --sda GP12 --scl GP13
```
결과 패널에 `0x42` 가 포함되어야 한다.

**Step 3 — Board A → Board B 메모리 쓰기 (reg 0x00 에 16바이트)**
```sh
# Terminal 1
replx COM1 i2c write 0x42 00 DE AD BE EF CA FE BA BE 01 02 03 04 05 06 07 08
```

**Step 4 — Board B: 타깃 메모리 확인**
```sh
# Terminal 2
replx COM2 i2c read 0x00 16
```
결과: `DE AD BE EF CA FE BA BE 01 02 03 04 05 06 07 08` (offset 0~15)

**Step 5 — Board B: 메모리 0x10에 데이터 프리로드 (컨트롤러가 읽을 것)**
```sh
# Terminal 2
replx COM2 i2c write 0x10 A1 A2 A3 A4 B1 B2 B3 B4
```

**Step 6 — Board A: 0x10 레지스터에서 8바이트 읽기**
```sh
# Terminal 1
replx COM1 i2c read 0x42 8 10
```
결과: `A1 A2 A3 A4 B1 B2 B3 B4`

**확인**: Step 4·6 모두 예상 데이터와 일치해야 한다.

> I2C-2 ~ I2C-4 는 Board B 타깃이 열린 상태를 유지한다. I2C-1 에서 `close` 하지 않고 다음 시나리오로 진행한다.

---

### I2C-2: Write-Readback 무결성 검증

I2C 에는 UART 루프백이나 SPI xfer 같은 에코 메커니즘이 없다. 컨트롤러가 타깃 메모리에 데이터를 쓴 뒤 동일 오프셋에서 읽어 비교하는 Write-Readback 방식으로 데이터 무결성을 검증한다.

**전제**: I2C-1 완료 상태 (Board B 타깃 활성)

**Step 1 — 순차 패턴 16바이트**
```sh
# Terminal 1
replx COM1 i2c write 0x42 00 00 01 02 03 04 05 06 07 08 09 0A 0B 0C 0D 0E 0F
replx COM1 i2c read 0x42 16 00
```
결과: `00 01 02 03 04 05 06 07 08 09 0A 0B 0C 0D 0E 0F`

**Step 2 — All-FF 패턴**
```sh
# Terminal 1
replx COM1 i2c write 0x42 00 FF FF FF FF FF FF FF FF FF FF FF FF FF FF FF FF
replx COM1 i2c read 0x42 16 00
```
결과: 16바이트 모두 `FF`

**Step 3 — All-00 패턴 (초기화 확인)**
```sh
# Terminal 1
replx COM1 i2c write 0x42 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
replx COM1 i2c read 0x42 16 00
```
결과: 16바이트 모두 `00`

**Step 4 — Board B 메모리 스냅샷으로 최종 상태 확인**
```sh
# Terminal 2
replx COM2 i2c mem
```
결과: 256바이트 hex+ASCII 덤프에서 offset 0x00~0x0F 이 모두 `00` (Step 3 결과)이고, 0x10~0x17 에 I2C-1 Step 5 에서 프리로드한 `A1 A2 A3 A4 B1 B2 B3 B4` 이 남아 있는 것을 확인한다.

**확인**: 매 Step 에서 write 한 데이터와 read 한 데이터가 바이트 단위로 완전히 일치해야 한다. 불일치 시 풀업 저항, 배선 길이, 클럭 주파수를 점검한다.

---

### I2C-3: 타깃 메모리 프리로드 + 레지스터 덤프

Board B 타깃이 각 오프셋에 구조화된 데이터를 미리 채워두고, Board A 가 `read` 및 `dump` 로 검증한다.  
메모리 레이아웃: 0x00–0x0F = 식별자, 0x10–0x1F = 상태, 0x20–0x2F = 데이터

**전제**: I2C-1 완료 상태 (Board B 타깃 활성)

**Step 1 — Board B: 메모리 구조 설정**
```sh
# Terminal 2 — 식별자 영역 (0x00)
replx COM2 i2c write 00 52 50 32 33 35 30 00 00 00 00 00 00 00 00 00 00
# Terminal 2 — 상태 영역 (0x10)
replx COM2 i2c write 10 01 00 00 00 FF 00 00 00 00 00 00 00 00 00 00 00
# Terminal 2 — 데이터 영역 (0x20)
replx COM2 i2c write 20 11 22 33 44 55 66 77 88 00 00 00 00 00 00 00 00
```

**Step 2 — Board B: 전체 메모리 스냅샷 확인**
```sh
# Terminal 2
replx COM2 i2c mem
```
결과: 256바이트 hex+ASCII 덤프. 0x00 행에 `RP2350`, 0x10 행에 `01 00 00 00 FF`, 0x20 행에 `11 22 33 44 55 66 77 88` 이 보여야 한다.

**Step 3 — Board A: 세 영역 순차 읽기**
```sh
# Terminal 1
replx COM1 i2c read 0x42 16 00   # 식별자
replx COM1 i2c read 0x42 16 10   # 상태
replx COM1 i2c read 0x42 16 20   # 데이터
```

**Step 4 — Board A: 레지스터 덤프 (0x00~0x2F)**
```sh
# Terminal 1
replx COM1 i2c dump 0x42 00 2F
```
결과: 16×N 그리드에 세 영역의 값이 일괄 표시된다. 프리로드 값과 대조하여 확인한다.

**Step 5 — Board A: 상태 영역 갱신 후 Board B 에서 확인**
```sh
# Terminal 1
replx COM1 i2c write 0x42 10 02 AA BB CC 00 00 00 00 00 00 00 00 00 00 00 00 00

# Terminal 2
replx COM2 i2c read 10 4
```
결과: `02 AA BB CC`

**확인**: 모든 영역 데이터가 프리로드한 값과 일치하고, Step 5 쓰기 후 Board B 읽기가 정상이어야 한다.

---

### I2C-4: 고속 400 kHz 반복 읽기

400 kHz 에서 1000회 반복 읽기를 수행하여 통신 안정성을 확인한다. 읽기 도중 타깃 메모리를 변경하여 라이브 업데이트도 검증한다.

**전제**: I2C-1 완료 상태 (Board B 타깃 활성)

**Step 1 — 400 kHz 지정, 1000회 반복 10 ms 간격**
```sh
# Terminal 1
replx COM1 i2c read 0x42 8 00 --freq 400000 -n 1000 --interval 10
```
기대 출력: 200줄 hex, 각 줄 `52 50 32 33 35 30 00 00` (또는 직전 프리로드 값)

**Step 2 — Board B 에서 도중에 데이터 변경 (동시 진행)**
```sh
# Terminal 2 (Step 1 실행 중)
replx COM2 i2c write 00 DE AD DE AD DE AD DE AD 00 00 00 00 00 00 00 00
```

**확인**: Board A 출력이 특정 시점부터 `DE AD DE AD DE AD DE AD` 로 바뀌어야 한다.  
OSError 없이 1000회가 완료되면 통신 안정성 합격.

**정리**
```sh
# Terminal 2
replx COM2 i2c close

# Terminal 1
replx COM1 i2c close
```

---

### I2C-5: 실전 하드웨어 — 센서 레지스터 읽기 (선택)

단일 보드에 연결된 I2C 센서를 스캔하고 레지스터를 읽는다.  
대표 소자: MPU-6500 (주소 0x68)

**연결**

```
Board A GP12 (SDA) ─── MPU-6500 SDA
Board A GP13 (SCL) ─── MPU-6500 SCL
3.3V ────────────────── MPU-6500 VCC
GND ─────────────────── MPU-6500 GND
                        AD0 → GND (주소 0x68)
```

---

**MPU-6500 주요 레지스터**

| 주소 | 이름 | R/W | 기본값 | 설명 |
|------|------|-----|--------|------|
| `0x19` | SMPLRT_DIV    | R/W | `0x00` | 샘플레이트 분주 (`rate = gyro_rate / (1 + div)`) |
| `0x1A` | CONFIG        | R/W | `0x00` | DLPF 설정 (bit2~0: DLPF_CFG) |
| `0x1B` | GYRO_CONFIG   | R/W | `0x00` | 자이로 풀스케일 (bit4~3: 0=250°/s, 1=500, 2=1000, 3=2000) |
| `0x1C` | ACCEL_CONFIG  | R/W | `0x00` | 가속도 풀스케일 (bit4~3: 0=2g, 1=4g, 2=8g, 3=16g) |
| `0x1D` | ACCEL_CONFIG2 | R/W | `0x00` | 가속도 DLPF (bit3: ACCEL_FCHOICE_B, bit1~0: A_DLPF) |
| `0x3B` | ACCEL_XOUT_H  | R   | —      | 가속도 X 상위 바이트 (0x3B~0x40: XYZ 각 2바이트) |
| `0x41` | TEMP_OUT_H    | R   | —      | 온도 상위 바이트 (0x41~0x42, $T=\frac{raw}{333.87}+21$°C) |
| `0x43` | GYRO_XOUT_H   | R   | —      | 자이로 X 상위 바이트 (0x43~0x48: XYZ 각 2바이트) |
| `0x6A` | USER_CTRL     | R/W | `0x00` | FIFO/I2C master/SIG_COND 리셋 |
| `0x6B` | PWR_MGMT_1    | R/W | `0x41` | SLEEP(bit6), CLKSEL(bit2~0) — 전원 인가 시 0x41(SLEEP) |
| `0x6C` | PWR_MGMT_2    | R/W | `0x00` | 축별 standby 비트 |
| `0x75` | WHO_AM_I      | R   | `0x70` | 칩 식별 (MPU-6050=0x68, MPU-6500=0x70, MPU-9250=0x71) |

---

**Step 1 — 버스 스캔**

```sh
replx COM1 i2c scan --sda GP12 --scl GP13
```

결과 패널에 `0x68` 이 포함되어야 한다.

---

**Step 2 — WHO_AM_I 읽기 (reg 0x75)**

```sh
replx COM1 i2c read 0x68 1 75
```

결과: `[70]` = MPU-6500

---

**Step 3 — 전원 관리 (reg 0x6B)**

전원 인가 직후 기본값 `0x41` (SLEEP=1). 깨어나려면 SLEEP 비트를 클리어해야 한다.

깨우기 (CLKSEL=0, 내부 8MHz):

```sh
replx COM1 i2c write 0x68 6B 00
replx COM1 i2c read  0x68 1 6B
```

절전 모드 진입 (SLEEP=1, CLKSEL=1=자이로PLL 권장):

```sh
replx COM1 i2c write 0x68 6B 41
replx COM1 i2c read  0x68 1 6B
```

Auto-select clock으로 깨우기 (CLKSEL=1):

```sh
replx COM1 i2c write 0x68 6B 01
replx COM1 i2c read  0x68 1 6B
```

---

**Step 4 — 가속도 풀스케일 설정 (reg 0x1C)**

```sh
replx COM1 i2c read  0x68 1 1C        # 현재 설정 확인
replx COM1 i2c write 0x68 1C 00       # 2g (기본값)
replx COM1 i2c write 0x68 1C 08       # 4g  (bit4~3 = 01)
replx COM1 i2c write 0x68 1C 10       # 8g  (bit4~3 = 10)
replx COM1 i2c write 0x68 1C 18       # 16g (bit4~3 = 11)
```

2g 기본값 기준 감도:  
$scale = \frac{2^{15}}{2} = 16384 \text{ LSB/g}$

---

**Step 5 — 가속도 데이터 읽기 (reg 0x3B, 6 바이트)**

```sh
replx COM1 i2c read  0x68 6 3B        # ACCEL_XOUT_H ~ ACCEL_ZOUT_L
```

ACCEL_X(2byte), ACCEL_Y(2byte), ACCEL_Z(2byte) — 각각 빅엔디언 signed 16-bit.  
$X=\frac{ACCEL\_X}{scale}$, $Y=\frac{ACCEL\_Y}{scale}$, $Z=\frac{ACCEL\_Z}{scale}$  
검증: $\sqrt{X^2 + Y^2 + Z^2}\approx1$ (정지 상태)

---

**Step 6 — 자이로 풀스케일 설정 (reg 0x1B)**

```sh
replx COM1 i2c read  0x68 1 1B        # 현재 설정 확인
replx COM1 i2c write 0x68 1B 00       # 250°/s  (기본값)
replx COM1 i2c write 0x68 1B 08       # 500°/s
replx COM1 i2c write 0x68 1B 10       # 1000°/s
replx COM1 i2c write 0x68 1B 18       # 2000°/s
```

250°/s 기본값 기준 감도:  
$scale = \frac{2^{15}}{250} = 131.07 \text{ LSB/(°/s)}$

---

**Step 7 — 자이로 데이터 읽기 (reg 0x43, 6 바이트)**

```sh
replx COM1 i2c read  0x68 6 43        # GYRO_XOUT_H ~ GYRO_ZOUT_L
```

---

**Step 8 — 온도 읽기 (reg 0x41, 2 바이트)**

```sh
replx COM1 i2c read  0x68 2 41
```

raw값(signed 16-bit) → $T = \frac{raw}{333.87} + 21$ °C

---

**Step 9 — 레지스터 덤프 (0x00~0x7F)**

```sh
replx COM1 i2c dump 0x68
```

0x00~0x7F 범위 레지스터 값이 16×8 그리드로 표시된다. 읽기 불가 레지스터는 `--` 로 표시된다.

---

**Step 10 — 반복 읽기 (100회, 500ms 간격)**

```sh
replx COM1 i2c read 0x68 6 3B -n 100 --interval 500
```

각 줄에 `[XX XX XX XX XX XX]` 6바이트 hex 출력. 보드를 움직이면 값이 변해야 한다.

---

### I2C-6: 실전 하드웨어 - HD44780 TextLCD + PCF8574 I2C(선택)

**연결**
```
MCU (RP2350)
  SDA ─────────────────┐
  SCL ─────────────────┤  PCF8574        HD44780
                       │  P7(D7) ──────► D7
                       │  P6(D6) ──────► D6
                       │  P5(D5) ──────► D5
                       │  P4(D4) ──────► D4   (4-bit mode: D3-D0 미사용)
                       │  P3(BL) ──────► 백라이트
                       │  P2(EN) ──────► Enable (래치 클록)
                       │  P1(RW) ──────► R/W   (항상 GND=쓰기)
                       └► P0(RS) ──────► RS    (0=명령, 1=데이터)
```

PCF8574는 I2C 1바이트 수신 → 8핀 동시 출력. 따라서 MCU가 보내는 1바이트 = HD44780 핀 8개의 상태.

HD44780은 EN↓(falling edge)에서 D7-D4를 읽으므로, nibble 하나를 전송하려면 EN↑ 바이트 → EN↓ 바이트 2회 writeto가 필요하다.

---

**PCF8574 바이트 구조**

```
bit:  7    6    5    4  │  3    2    1    0
      D7   D6   D5   D4 │  BL   EN   RW   RS
      ────────────────── │ ──────────────────
      HD44780 상위 nibble│  제어 핀
```

| 핀 | 비트 | 설명 |
|----|------|------|
| D7-D4 | bit7-4 | HD44780 데이터 버스 상위 nibble |
| BL | bit3 | 백라이트 (1=ON) |
| EN | bit2 | Enable — 이 핀이 1→0 될 때 D7-D4 래치 |
| RW | bit1 | Read/Write — 항상 0(쓰기) |
| RS | bit0 | Register Select — 0=명령, 1=데이터 |

바이트 계산 공식은 다음과 같다.

```
cmd  EN↑ = (nibble << 4) | 0x0C    (BL=1, EN=1, RW=0, RS=0)
cmd  EN↓ = (nibble << 4) | 0x08    (BL=1, EN=0, RW=0, RS=0)
data EN↑ = (nibble << 4) | 0x0D    (BL=1, EN=1, RW=0, RS=1)
data EN↓ = (nibble << 4) | 0x09    (BL=1, EN=0, RW=0, RS=1)
```

예) nibble=0x2, RS=0(cmd):
- EN↑ = `(0x2 << 4) | 0x0C` = `0x20 | 0x0C` = **`0x2C`**
- EN↓ = `(0x2 << 4) | 0x08` = `0x20 | 0x08` = **`0x28`**

예) nibble=0x4, RS=1(data 'H' 상위):
- EN↑ = `(0x4 << 4) | 0x0D` = `0x40 | 0x0D` = **`0x4D`**
- EN↓ = `(0x4 << 4) | 0x09` = `0x40 | 0x09` = **`0x49`**

---

**1바이트 전송 구조**

**cmd `0x28` 전송 예시**

`0x28` = `0010 1000` → upper=0x2, lower=0x8

```sh
replx i2c seq 27  2C 28  8C 88
```

| 토큰 | 바이트      | D7-D4 | BL | EN | RW | RS | 의미 |
|------|------------|-------|----|----|----|----|------|
| `2C` | 0010 1100  | 0010  |  1 |  1 |  0 |  0 | 상위 0x2, EN↑ |
| `28` | 0010 1000  | 0010  |  1 |  0 |  0 |  0 | 상위 0x2, EN↓ → **0x2 래치** |
| `8C` | 1000 1100  | 1000  |  1 |  1 |  0 |  0 | 하위 0x8, EN↑ |
| `88` | 1000 1000  | 1000  |  1 |  0 |  0 |  0 | 하위 0x8, EN↓ → **0x8 래치** |

→ HD44780이 수신한 값: **0x28** = Function Set (N=1: 2-line, F=0: 5×8 font)

**data `0x48` ('H') 전송 예시**

`0x48` = `0100 1000` → upper=0x4, lower=0x8

```sh
replx i2c seq 27  4D 49  8D 89
```

| 토큰 | 바이트      | D7-D4 | BL | EN | RW | RS | 의미 |
|------|------------|-------|----|----|----|----|------|
| `4D` | 0100 1101  | 0100  |  1 |  1 |  0 |  1 | 상위 0x4, EN↑ |
| `49` | 0100 1001  | 0100  |  1 |  0 |  0 |  1 | 상위 0x4, EN↓ → **0x4 래치** |
| `8D` | 1000 1101  | 1000  |  1 |  1 |  0 |  1 | 하위 0x8, EN↑ |
| `89` | 1000 1001  | 1000  |  1 |  0 |  0 |  1 | 하위 0x8, EN↓ → **0x8 래치** |

→ DDRAM에 `0x48`='H' 기록 (RS=1이므로 데이터 레지스터)

---

**타이밍**

```
I2C writeto() 1회 ≈ 180–200µs  (100kHz, 2바이트 + 오버헤드)
HD44780 EN 최소 펄스폭 = 230ns  → I2C 1회 전송으로 충분히 충족
HD44780 일반 명령 실행 시간 = 37µs  → I2C 오버헤드로 충분
HD44780 Clear/Home 실행 시간 = 1.52ms → 명시적 m2 대기 필요
```

---

**스캔**

```sh
replx i2c scan --sda gp12 --scl gp13
```

> **freq 기본값 400kHz 사용 가능.**  
> PCF8574 데이터시트 공칭 최대는 100kHz이나, 실제로는 400kHz에서도 정상 동작이 확인되는데, I2C 타깃 응답 속도보다 PCF8574의 출력 핀 전파 지연(tpd가 약 300ns)이 짧아서, EN 펄스 타이밍 여유가 충분하기 때문이다. 
> 문제가 발생하면 `--freq 100000`으로 낮춰 시도.

PCF8574의 기본 주소: `0x27` (A2=A1=A0=1), 최소 `0x20`~`0x27`.  
PCF8574**A**는 `0x38`~`0x3F`.

---

**Init: 4-bit mode 전환 (Datasheet Figure 24)**

8-bit mode로 시작하는 HD44780을 4-bit mode로 전환하는 특수 시퀀스.  
이 단계에서는 아직 4-bit mode가 아니므로 **nibble 1개씩** 전송한다.

```sh
replx i2c seq 27  m50  3C 38 m5  3C 38 u120  3C 38  2C 28
```

| 토큰   | 바이트      | D7-D4 | BL | EN | RS | 의미 |
|-------|-----------|-------|----|----|----|------|
| `m50` | —         | —     |  — |  — |  — | VCC 안정화 대기 >40ms |
| `3C`  | 0011 1100 | 0011  |  1 |  1 |  0 | nibble=0x3, EN↑ |
| `38`  | 0011 1000 | 0011  |  1 |  0 |  0 | nibble=0x3, EN↓ → 1st 0x3 래치 |
| `m5`  | —         | —     |  — |  — |  — | >4.1ms 대기 (데이터시트 명시) |
| `3C`  | 0011 1100 | 0011  |  1 |  1 |  0 | nibble=0x3, EN↑ |
| `38`  | 0011 1000 | 0011  |  1 |  0 |  0 | nibble=0x3, EN↓ → 2nd 0x3 래치 |
| `u120`| —         | —     |  — |  — |  — | >100µs 대기 |
| `3C`  | 0011 1100 | 0011  |  1 |  1 |  0 | nibble=0x3, EN↑ |
| `38`  | 0011 1000 | 0011  |  1 |  0 |  0 | nibble=0x3, EN↓ → 3rd 0x3 래치 |
| `2C`  | 0010 1100 | 0010  |  1 |  1 |  0 | nibble=0x2, EN↑ |
| `28`  | 0010 1000 | 0010  |  1 |  0 |  0 | nibble=0x2, EN↓ → **4-bit mode 확정** |

> **왜 0x3을 세 번?**  
> HD44780 전원 인가 직후 내부 상태가 불확정. 0x3(=8-bit mode set)을 세 번 보내  
> 8-bit/4-bit 어느 상태였든 확실히 8-bit mode로 만든 뒤 0x2를 보내 4-bit로 전환.

---

**초기화 명령 순서**

```sh
# 1. Function Set: 4-bit, 2-line, 5×8
replx i2c seq 27  2C 28 8C 88

# 2. Display ON, Cursor OFF, Blink OFF
replx i2c seq 27  0C 08 CC C8

# 3. Clear Display  (실행 >1.52ms → m2)
replx i2c seq 27  0C 08 1C 18 m2

# 4. Entry Mode: 커서 우→좌 이동, 디스플레이 시프트 없음
replx i2c seq 27  0C 08 6C 68
```

**cmd 바이트 계산표**

| HD44780 cmd | binary    | upper | lower | seq 토큰 |
|-------------|-----------|-------|-------|----------|
| `0x01` Clear       | 0000 0001 | 0x0 | 0x1 | `0C 08 1C 18 m2` |
| `0x02` Home        | 0000 0010 | 0x0 | 0x2 | `0C 08 2C 28 m2` |
| `0x06` Entry Mode  | 0000 0110 | 0x0 | 0x6 | `0C 08 6C 68` |
| `0x0C` Display ON  | 0000 1100 | 0x0 | 0xC | `0C 08 CC C8` |
| `0x0E` Cursor ON   | 0000 1110 | 0x0 | 0xE | `0C 08 EC E8` |
| `0x0F` Cursor+Blink| 0000 1111 | 0x0 | 0xF | `0C 08 FC F8` |
| `0x28` Function Set| 0010 1000 | 0x2 | 0x8 | `2C 28 8C 88` |
| `0x80` DDRAM 0x00  | 1000 0000 | 0x8 | 0x0 | `8C 88 0C 08` |
| `0xC0` DDRAM 0x40  | 1100 0000 | 0xC | 0x0 | `CC C8 0C 08` |

---

**단독 cmd**

```sh
# Clear
replx i2c seq 27  0C 08 1C 18 m2

# Return Home
replx i2c seq 27  0C 08 2C 28 m2

# 커서 밑줄 ON
replx i2c seq 27  0C 08 EC E8

# 커서 밑줄 + 깜빡임 ON
replx i2c seq 27  0C 08 FC F8

# 커서 OFF
replx i2c seq 27  0C 08 CC C8

# 1행 이동 (DDRAM addr 0x00)
replx i2c seq 27  8C 88 0C 08

# 2행 이동 (DDRAM addr 0x40)
replx i2c seq 27  CC C8 0C 08
```

---

**문자 출력 — 바이트 계산**

**data 바이트 계산표**

RS=1이므로 EN↑/EN↓ 하위 4비트가 `0x0D`/`0x09` (BL+RS, BL+EN 없음)

| 문자 | ASCII | binary    | upper | lower | seq 토큰 |
|------|-------|-----------|-------|-------|----------|
| `' '` | 0x20 | 0010 0000 | 0x2 | 0x0 | `2D 29 0D 09` |
| `'0'` | 0x30 | 0011 0000 | 0x3 | 0x0 | `3D 39 0D 09` |
| `'9'` | 0x39 | 0011 1001 | 0x3 | 0x9 | `3D 39 9D 99` |
| `'A'` | 0x41 | 0100 0001 | 0x4 | 0x1 | `4D 49 1D 19` |
| `'H'` | 0x48 | 0100 1000 | 0x4 | 0x8 | `4D 49 8D 89` |
| `'I'` | 0x49 | 0100 1001 | 0x4 | 0x9 | `4D 49 9D 99` |
| `'Z'` | 0x5A | 0101 1010 | 0x5 | 0xA | `5D 59 AD A9` |
| `'a'` | 0x61 | 0110 0001 | 0x6 | 0x1 | `6D 69 1D 19` |
| `'i'` | 0x69 | 0110 1001 | 0x6 | 0x9 | `6D 69 9D 99` |
| `'z'` | 0x7A | 0111 1010 | 0x7 | 0xA | `7D 79 AD A9` |

**임의 문자 공식**

```
char_code = ord('X')
upper = char_code >> 4
lower = char_code & 0xF

EN↑_upper = (upper << 4) | 0x0D
EN↓_upper = (upper << 4) | 0x09
EN↑_lower = (lower << 4) | 0x0D
EN↓_lower = (lower << 4) | 0x09
```

---

**전체 흐름 예제**

```sh
# 1. 스캔 & 버스 저장 (400kHz 기본값 사용)
replx i2c scan --sda 12 --scl 13

# 2. 4-bit mode 전환
replx i2c seq 27  m50  3C 38 m5  3C 38 u120  3C 38  2C 28

# 3. Function Set
replx i2c seq 27  2C 28 8C 88

# 4. Display ON
replx i2c seq 27  0C 08 CC C8

# 5. Clear
replx i2c seq 27  0C 08 1C 18 m2

# 6. Entry Mode
replx i2c seq 27  0C 08 6C 68

# 7. 'H' 출력
replx i2c seq 27  4D 49 8D 89

# 8. 'i' 출력
replx i2c seq 27  6D 69 9D 99

# 9. 2행으로 커서 이동
replx i2c seq 27  CC C8 0C 08

# 10. 'replx' 출력
replx i2c seq 27 7D 79 2D 29   # 'r' (0x72)
replx i2c seq 27 6D 69 5D 59   # 'e' (0x65)
replx i2c seq 27 7D 79 0D 09   # 'p' (0x70)
replx i2c seq 27 6D 69 CD C9   # 'l' (0x6C)
replx i2c seq 27 7D 79 8D 89   # 'x' (0x78)
```

## 참고: 핀 번호 규칙

| 보드 | GPIO | UART | SPI | I2C |
|---|---|---|---|---|
| RP2350 | GP0 ~ GP47 | UART0: GP0/GP1, GP12/GP13, GP16/GP17, GP28/GP29 | SPI0: GP2/GP3/GP4, SPI1: GP10/GP11/GP12 | I2C0: GP(4n)/GP(4n+1) e.g. GP12/GP13, I2C1: GP(4n+2)/GP(4n+3) e.g. GP18/GP19 |
| ESP32 | GPIO0 ~ | — | CH=1 고정 | 임의 GPIO, CH=0 |

SPI 슬레이브는 **RP2350 전용**이며, PIO + DMA 를 사용하므로 다른 MicroPython 클래스와 핀이 충돌하지 않아야 한다.

I2C 타깃(`--target`) 모드는 `machine.I2CTarget` (MicroPython v1.26 이상)을 사용하며, 플랫폼 비의존적이다.  
버스 양 끝에 **4.7 kΩ 풀업 저항**이 필요하며, 두 보드 중 한쪽에만 달아도 된다.
