// Template player for a side-on 2D Unity game, scaffolded by Builders Gate.
//
// Every tunable a playtester is likely to complain about is a public field AND
// is emitted as telemetry, so "the jump feels floaty" lands next to the numbers
// that made it feel that way. Same feel model as the Godot template: fall
// faster than you rise, coyote time, a jump buffer.
//
// Compiles under either input backend: the Input System package when the
// project has switched to it, the legacy Input Manager otherwise.

using UnityEngine;
#if ENABLE_INPUT_SYSTEM && !ENABLE_LEGACY_INPUT_MANAGER
using UnityEngine.InputSystem;
#endif

[RequireComponent(typeof(Rigidbody2D))]
public class PlayerController2D : MonoBehaviour
{
    public float speed = 7f;
    public float jumpVelocity = 13f;
    // Falling faster than rising is the single biggest lever on "floaty".
    public float fallMultiplier = 1.6f;
    // Forgiveness windows. Their absence reads as "the jump didn't register".
    public float coyoteTime = 0.10f;
    public float jumpBuffer = 0.10f;

    Rigidbody2D _body;
    ContactFilter2D _ground;
    float _coyote;
    float _buffer;
    bool _wasGrounded;
    float _airStart;
    float _peakY;
    float _startY;

    void Awake()
    {
        _body = GetComponent<Rigidbody2D>();
        // Ground = a contact whose normal points up (within 45 degrees).
        _ground = new ContactFilter2D();
        _ground.SetNormalAngle(45f, 135f);
    }

    void Update()
    {
        if (JumpPressed()) _buffer = jumpBuffer;
        else _buffer = Mathf.Max(0f, _buffer - Time.deltaTime);
    }

    void FixedUpdate()
    {
        bool grounded = _body.IsTouching(_ground);
        _coyote = grounded ? coyoteTime : Mathf.Max(0f, _coyote - Time.fixedDeltaTime);

        var v = _body.linearVelocity;
        v.x = Axis() * speed;

        if (_buffer > 0f && _coyote > 0f)
        {
            v.y = jumpVelocity;
            _buffer = 0f;
            _coyote = 0f;
            _airStart = Time.time;
            _startY = _peakY = transform.position.y;
            BGate.BGateTelemetry.Emit("jump", ("jump_velocity", jumpVelocity),
                ("fall_multiplier", fallMultiplier), ("from_coyote", !grounded));
        }
        if (v.y < 0f)
            v.y += Physics2D.gravity.y * _body.gravityScale * (fallMultiplier - 1f) * Time.fixedDeltaTime;
        _body.linearVelocity = v;

        if (!grounded) _peakY = Mathf.Max(_peakY, transform.position.y);
        if (grounded && !_wasGrounded && _airStart > 0f)
        {
            BGate.BGateTelemetry.Emit("land", ("air_time", Time.time - _airStart),
                ("peak_height", _peakY - _startY));
            _airStart = 0f;
        }
        _wasGrounded = grounded;
    }

    static float Axis()
    {
#if ENABLE_INPUT_SYSTEM && !ENABLE_LEGACY_INPUT_MANAGER
        var k = Keyboard.current;
        if (k == null) return 0f;
        float x = 0f;
        if (k.aKey.isPressed || k.leftArrowKey.isPressed) x -= 1f;
        if (k.dKey.isPressed || k.rightArrowKey.isPressed) x += 1f;
        return x;
#else
        return Input.GetAxisRaw("Horizontal");
#endif
    }

    static bool JumpPressed()
    {
#if ENABLE_INPUT_SYSTEM && !ENABLE_LEGACY_INPUT_MANAGER
        var k = Keyboard.current;
        return k != null && (k.spaceKey.wasPressedThisFrame || k.wKey.wasPressedThisFrame
                             || k.upArrowKey.wasPressedThisFrame);
#else
        return Input.GetButtonDown("Jump") || Input.GetKeyDown(KeyCode.W)
               || Input.GetKeyDown(KeyCode.UpArrow);
#endif
    }
}
