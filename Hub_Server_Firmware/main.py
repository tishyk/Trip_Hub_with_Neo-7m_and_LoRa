"""
main.py - Pico A entry point.

Just two steps:
    1. Run hardware tests (from hardware_test.py)
    2. Hand off to lora_bridge.run() which loops forever

All actual bridge logic lives in lora_bridge.py.
"""

import time
from machine import Timer

import config
import leds
import lora_bridge


# Module-level reference for the post-test LED-off timer.  Without keeping
# a reference, MicroPython's GC frees the Timer object before the 8-second
# countdown elapses and the callback never runs (-> LEDs stay lit forever).
_off_timer = None


# ============================================================
# 1. Run hardware tests
# ============================================================
def run_tests():
    import hardware_test as ht
    print("\n" + "#" * 50)
    print("# LoRa Bridge - Tests + Bridge Loop")
    print("#" * 50)

    ht.status_off()
    results = {}
    results["OLED"] = ht.run_test(ht.test_i2c_oled, "OLED")

    ht.banner("TEST: LEDs (6x)")
    ht.oled_show_status("LEDs", 'RUN')
    results["LEDs"] = ht.test_leds()
    ht.status_set(results["LEDs"])
    ht.oled_show_status("LEDs", 'OK' if results["LEDs"] else 'FAIL')
    time.sleep(0.8)

    results["Buzzer"]  = ht.run_test(ht.test_buzzer,  "Buzzer")
    results["LoRa"]    = ht.run_test(ht.test_lora,    "LoRa SX1278")
    results["DS1302"]  = ht.run_test(ht.test_ds1302,  "DS1302 RTC")
    results["HC-SR04"] = ht.run_test(ht.test_hcsr04,  "HC-SR04")
    results["Buttons"] = ht.run_test(ht.test_buttons, "Buttons")

    ht.banner("ALL TESTS DONE")
    for name, passed in results.items():
        print("  {:10s} : {}".format(name, "PASS" if passed else "FAIL"))

    all_passed = all(results.values())
    ht.status_set(all_passed)

    # Smile/frown summary
    if ht._oled is not None:
        try:
            ht._oled.fill(0)
            cx = 64; r = 56
            target_depth = config.OLED_HEIGHT - 8
            depth_ratio = target_depth / r
            if all_passed:
                ht.draw_smiley(ht._oled, cx=cx, cy=8, r=r,
                               happy=True, depth_ratio=depth_ratio)
            else:
                ht.draw_smiley(ht._oled, cx=cx, cy=config.OLED_HEIGHT - 8,
                               r=r, happy=False, depth_ratio=depth_ratio)
            ht._oled.show()
            time.sleep(2)
        except Exception:
            pass

    # Turn status LEDs off 8s later (= 10s total post-test).
    # Keep the Timer in a module-level variable so MicroPython's GC doesn't
    # collect it before it fires.
    global _off_timer
    _off_timer = Timer()
    _off_timer.init(period=8000, mode=Timer.ONE_SHOT,
                    callback=lambda _: leds.all_off())

    return all_passed


# ============================================================
# Entry
# ============================================================
def main():
    run_tests()
    lora_bridge.run()


main()