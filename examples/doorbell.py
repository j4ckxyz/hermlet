"""Tell your agent when something happens, with a picture.

Wire a button to a GPIO pin and a camera to the Pi; when the button is pressed
the agent gets a photo and decides what to do about it (message you on
Discord, write it down, ignore the postman).

    pip install gpiozero        # already on Raspberry Pi OS
    python examples/doorbell.py
"""
import subprocess
import tempfile
import time

import hermlet

BUTTON_PIN = 17


def take_photo() -> str:
    path = tempfile.mktemp(suffix=".jpg")
    subprocess.run(["rpicam-still", "-n", "-t", "500", "--width", "1280", "--height", "720", "-o", path], check=True)
    return path


def main() -> None:
    from gpiozero import Button

    h = hermlet.connect()
    button = Button(BUTTON_PIN, bounce_time=0.2)
    print("waiting for the doorbell ...")
    while True:
        button.wait_for_press()
        try:
            answer = h.ask("Someone rang the doorbell just now. Here is the camera picture. "
                           "Tell me in one line who or what you see.", files=[take_photo()])
            print(answer)
        except hermlet.HermletError as exc:
            print("could not reach Hermes:", exc)
        time.sleep(5)


if __name__ == "__main__":
    main()
