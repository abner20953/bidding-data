document.addEventListener('DOMContentLoaded', () => {

    // 1. 导航栏滚动吸顶颜色变化
    const navbar = document.getElementById('navbar');

    window.addEventListener('scroll', () => {
        if (window.scrollY > 50) {
            navbar.classList.add('scrolled');
        } else {
            navbar.classList.remove('scrolled');
        }
    });

    // 触发一次以防初始就在下方
    if (window.scrollY > 50) {
        navbar.classList.add('scrolled');
    }

    // 2. 滚动出现动效 (Scroll Reveal)
    const reveals = document.querySelectorAll('.reveal');

    const revealOnScroll = () => {
        const windowHeight = window.innerHeight;
        const elementVisible = 100;

        reveals.forEach((reveal) => {
            const elementTop = reveal.getBoundingClientRect().top;
            if (elementTop < windowHeight - elementVisible) {
                reveal.classList.add('active');
            }
        });
    };

    window.addEventListener('scroll', revealOnScroll);
    revealOnScroll(); // 初始化

    // 3. 数字滚动动效 (Number Counter)
    const counters = document.querySelectorAll('.stat-number');
    let hasCounted = false;

    const runCounter = () => {
        const statsSection = document.getElementById('clinical');
        if (!statsSection) return;

        const sectionTop = statsSection.getBoundingClientRect().top;
        if (sectionTop < window.innerHeight - 100 && !hasCounted) {
            hasCounted = true;
            counters.forEach(counter => {
                const target = parseFloat(counter.getAttribute('data-target'));
                const duration = 2000; // 2 seconds
                const stepTime = 20;
                let current = 0;

                // 简单的判断是否包含小数
                const isFloat = target % 1 !== 0;

                // 根据目标值推算每次步进距离
                const increment = target / (duration / stepTime);

                const timer = setInterval(() => {
                    current += increment;
                    if (current >= target) {
                        counter.innerText = target;
                        clearInterval(timer);
                    } else {
                        counter.innerText = isFloat ? current.toFixed(1) : Math.floor(current);
                    }
                }, stepTime);
            });
        }
    };

    window.addEventListener('scroll', runCounter);

    // 4. 锚点平滑滚动
    document.querySelectorAll('a[href^="#"]').forEach(anchor => {
        anchor.addEventListener('click', function (e) {
            e.preventDefault();
            const targetId = this.getAttribute('href');
            if (targetId === '#') return;

            const targetElement = document.querySelector(targetId);
            if (targetElement) {
                const navHeight = navbar.offsetHeight;
                window.scrollTo({
                    top: targetElement.offsetTop - navHeight,
                    behavior: 'smooth'
                });
            }
        });
    });

});
